"""Mantém o estado da moldura e do atalho de parada.

Exibe o agente, o atalho e as falas recentes durante o uso da tela.
Ao parar, bloqueia novos gestos, solta entradas pressionadas, cancela o turno
e fecha a moldura, mesmo quando uma das etapas falha.
O backend do sistema desenha a imagem e escuta o atalho.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Mapping, Optional, Protocol, Tuple

logger = logging.getLogger(__name__)

# A nova embaixo em 1; as anteriores sobem em 0,6 e 0,3; a quarta some.
OPACIDADES: Tuple[float, ...] = (1.0, 0.6, 0.3)

# Três modificadores e uma letra: nenhum
# programa comum usa, e não se aperta por acidente.
ATALHO_PADRAO = "ctrl+alt+shift+s"

_MODIFICADORES = ("ctrl", "alt", "shift", "win")
_APELIDOS = {"control": "ctrl", "ctl": "ctrl", "option": "alt", "opt": "alt",
             "super": "win", "meta": "win", "cmd": "win", "command": "win"}
_NOMES = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
_LIMITE_DA_FALA = 120
# A onda do clique é um aviso do instante; o balão dura um pouco mais. Os dois
# somem sozinhos: parado no ponto do gesto, o balão parecia a tela travada.
ONDA_SEGUNDOS = 0.7
BALAO_SEGUNDOS = 3.0


class MolduraParada(RuntimeError):
    """A pessoa apertou o atalho: nenhum gesto roda depois disso."""


def normalizar_atalho(texto: str) -> str:
    """`Ctrl + Shift + Alt + S` → `ctrl+alt+shift+s`.

    Pede pelo menos DOIS modificadores e uma tecla: com um só, o atalho
    colide com o que os programas já usam (`ctrl+s` salva) ou se aperta sem
    querer. A ordem dos modificadores é fixa, para comparar dois atalhos.
    """
    partes = [p.strip().lower() for p in str(texto or "").split("+") if p.strip()]
    partes = [_APELIDOS.get(p, p) for p in partes]
    modificadores = [m for m in _MODIFICADORES if m in partes]
    teclas = [p for p in partes if p not in _MODIFICADORES]
    if len(teclas) != 1 or len(modificadores) < 2 or len(set(partes)) != len(partes):
        raise ValueError(f"atalho de parar inválido: {texto!r} (dois modificadores e uma tecla)")
    return "+".join(modificadores + teclas)


def atalho_de_parar() -> str:
    """Lê o atalho configurado e usa o padrão quando ele é inválido."""
    bruto = os.environ.get("TATO_COMPUTADOR_ATALHO")
    if not bruto:
        return ATALHO_PADRAO
    try:
        return normalizar_atalho(bruto)
    except ValueError as exc:
        logger.warning("%s; usando %s", exc, ATALHO_PADRAO)
        return ATALHO_PADRAO


def teclas_do_atalho(atalho: str) -> str:
    """`ctrl+alt+shift+s` → `Ctrl+Alt+Shift+S`, como vai na teclinha da pílula."""
    return "+".join(_NOMES.get(p, p.upper()) for p in atalho.split("+"))


# A moldura usa o nome informado pelo cliente MCP conectado.
_AGENTE = "Tato"
_NOMES_DE_CLIENTE = {"claude-code": "Claude Code", "claude code": "Claude Code", "codex": "Codex",
                     "codex-mcp-client": "Codex", "cursor": "Cursor", "cursor-vscode": "Cursor",
                     "vscode": "VS Code", "visual studio code": "VS Code", "windsurf": "Windsurf",
                     "zed": "Zed", "gemini-cli": "Gemini CLI", "sentury": "Sentury"}


def usar_agente(nome: str) -> None:
    """O nome que a moldura mostra; vazio vira "Agente"."""
    global _AGENTE
    _AGENTE = " ".join(str(nome or "").split())[:40] or "Agente"


def agente() -> str:
    return _AGENTE


def nome_do_cliente(nome: str) -> str:
    """O nome que o cliente MCP informa (`clientInfo.name`) do jeito que a pessoa conhece."""
    bruto = " ".join(str(nome or "").split())
    if not bruto:
        return "Agente"
    return _NOMES_DE_CLIENTE.get(bruto.lower()) or bruto.replace("-", " ").replace("_", " ").title()


def texto_do_selo(atalho: str) -> str:
    teclas = " + ".join(_NOMES.get(p, p.upper()) for p in atalho.split("+"))
    return f"{_AGENTE} está usando o computador · para parar, pressione {teclas}"


@dataclass(frozen=True)
class Fala:
    texto: str
    opacidade: float


@dataclass(frozen=True)
class Quadro:
    """Estado visual da moldura, das falas e do indicador do gesto.

    `falas` segue da mais antiga à mais recente.
    `ponto` posiciona o indicador sobre o alvo sem mover o mouse.
    """

    aberta: bool
    selo: str
    falas: Tuple[Fala, ...]
    reduzir_movimento: bool
    atalho: str = ""
    etiqueta: str = ""
    clique: bool = False
    contexto: str = ""
    agente: str = "Tato"
    ponto: Optional[Tuple[int, int]] = None


class Backend(Protocol):
    def desenhar(self, quadro: Quadro) -> None: ...
    def ouvir_atalho(self, atalho: str, ao_apertar: Callable[[], None]) -> None: ...
    def parar_de_ouvir(self) -> None: ...
    def soltar_entrada(self) -> None: ...


def _uma_linha(texto: Any) -> str:
    texto = " ".join(str(texto or "").split())
    return texto if len(texto) <= _LIMITE_DA_FALA else texto[: _LIMITE_DA_FALA - 1] + "…"


class Moldura:
    """Abre no começo do turno com @computador, fecha com ele ou com o atalho.

    Uso: `with Moldura(backend, ao_parar=lambda: turno_em_curso.parar(sessao)) as m:`
    e `m.conferir()` antes de cada gesto.
    """

    def __init__(self, backend: Backend, ao_parar: Callable[[], Any], *,
                 atalho: Optional[str] = None, reduzir_movimento: bool = False, contexto: str = ""):
        self._backend = backend
        self._ao_parar = ao_parar
        self.atalho = normalizar_atalho(atalho) if atalho else atalho_de_parar()
        self._reduzir = reduzir_movimento
        self._falas: Deque[str] = deque(maxlen=len(OPACIDADES))
        self._trava = threading.RLock()
        self.aberta = False
        self.parada = False
        self._etiqueta = ""
        self._clique = False
        self._ponto: Optional[Tuple[int, int]] = None
        self._gestos = 0
        self.contexto = contexto

    def quadro(self) -> Quadro:
        with self._trava:
            falas = list(self._falas)
            opacidades = OPACIDADES[: len(falas)][::-1]
            return Quadro(
                aberta=self.aberta,
                selo=texto_do_selo(self.atalho) if self.aberta else "",
                falas=tuple(Fala(t, o) for t, o in zip(falas, opacidades)),
                reduzir_movimento=self._reduzir,
                atalho=teclas_do_atalho(self.atalho) if self.aberta else "",
                etiqueta=self._etiqueta if self.aberta else "",
                clique=self._clique if self.aberta else False,
                contexto=self.contexto,
                agente=_AGENTE,
                ponto=self._ponto if self.aberta else None,
            )

    def abrir(self) -> "Moldura":
        with self._trava:
            if self.parada:
                raise MolduraParada("a moldura já foi parada; abra outra para o próximo turno")
            if self.aberta:
                return self
            self._backend.ouvir_atalho(self.atalho, self.parar)
            self.aberta = True
            self._backend.desenhar(self.quadro())
            return self

    def falar(self, texto: Any) -> None:
        """Uma fala do narrador. Fechada ou parada, não aparece: a moldura
        não pode reabrir sozinha depois do atalho."""
        texto = _uma_linha(texto)
        with self._trava:
            if not texto or not self.aberta or self.parada:
                return
            if self._falas and self._falas[-1] == texto:
                return
            self._falas.append(texto)
            self._backend.desenhar(self.quadro())

    def gesto(self, etiqueta: str, clique: bool = False, ponto: Optional[Tuple[int, int]] = None) -> None:
        """O balão diz o gesto e o clique ganha a onda, os dois no `ponto` do
        gesto (onde o mouse clicou, ou o elemento que a acessibilidade
        acionou): fixos ali, sem seguir o mouse de quem usa. A onda some
        depois de `ONDA_SEGUNDOS` e o balão depois de `BALAO_SEGUNDOS`."""
        with self._trava:
            if not self.aberta or self.parada:
                return
            self._etiqueta, self._clique = _uma_linha(etiqueta)[:60], bool(clique)
            self._ponto = (int(ponto[0]), int(ponto[1])) if ponto else None
            self._gestos += 1
            numero = self._gestos
            self._backend.desenhar(self.quadro())
        for segundos, balao in ((ONDA_SEGUNDOS, False),) * bool(clique) + ((BALAO_SEGUNDOS, True),):
            timer = threading.Timer(segundos, self._apagar, args=(numero, balao))
            timer.daemon = True
            timer.start()

    def _apagar(self, numero: int, balao: bool) -> None:
        with self._trava:
            # Outro gesto chegou depois: o balão e a onda dele é que valem.
            if numero != self._gestos or not self.aberta or not (self._etiqueta if balao else self._clique):
                return
            if balao:
                self._etiqueta, self._clique, self._ponto = "", False, None
            else:
                self._clique = False
            self._backend.desenhar(self.quadro())

    def ouvir(self, evento: Mapping[str, Any]) -> None:
        """Um evento do turno: a `fala` vem no `tool_start`, a mesma que o
        chat mostra no passo."""
        if isinstance(evento, Mapping) and evento.get("type") == "tool_start" and evento.get("fala"):
            self.falar(evento["fala"])

    def conferir(self) -> None:
        """O executor chama antes de cada gesto."""
        if self.parada:
            raise MolduraParada("parado pelo atalho")

    def parar(self) -> None:
        """O atalho. Pode vir da thread do sistema que o ouviu."""
        with self._trava:
            if self.parada:
                return
            self.parada = True
        for etapa in (self._backend.soltar_entrada, self._ao_parar, self.fechar):
            try:
                etapa()
            except Exception:
                logger.warning("Etapa do parar falhou: %s", getattr(etapa, "__name__", etapa), exc_info=True)

    def fechar(self) -> None:
        with self._trava:
            if not self.aberta:
                return
            self.aberta = False
            self._falas.clear()
            try:
                self._backend.parar_de_ouvir()
            finally:
                self._backend.desenhar(self.quadro())

    def __enter__(self) -> "Moldura":
        return self.abrir()

    def __exit__(self, *exc: Any) -> None:
        self.fechar()


class BackendFalso:
    """O backend dos testes e de quem roda sem tela: guarda o que desenharia."""

    def __init__(self) -> None:
        self.quadros: list = []
        self.atalho: Optional[str] = None
        self._ao_apertar: Optional[Callable[[], None]] = None
        self.soltou = 0

    def desenhar(self, quadro: Quadro) -> None:
        self.quadros.append(quadro)

    def ouvir_atalho(self, atalho: str, ao_apertar: Callable[[], None]) -> None:
        self.atalho, self._ao_apertar = atalho, ao_apertar

    def parar_de_ouvir(self) -> None:
        self.atalho, self._ao_apertar = None, None

    def soltar_entrada(self) -> None:
        self.soltou += 1

    def apertar(self) -> None:
        """A pessoa apertou o atalho."""
        if self._ao_apertar is not None:
            self._ao_apertar()

    @property
    def ultimo(self) -> Optional[Quadro]:
        return self.quadros[-1] if self.quadros else None


__all__ = ["ATALHO_PADRAO", "Backend", "BackendFalso", "Fala", "Moldura", "MolduraParada",
           "OPACIDADES", "Quadro", "atalho_de_parar", "normalizar_atalho", "teclas_do_atalho", "texto_do_selo"]
