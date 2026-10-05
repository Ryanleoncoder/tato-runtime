"""Define o contrato comum dos backends de controle do computador.

Os backends Windows e X11 leem a tela, operam entradas e listam janelas.
Cada backend traduz os nomes de tecla compartilhados para o sistema.
A moldura e o atalho de parada são apresentados pelo backend da plataforma.
"""
from __future__ import annotations

import io
import os
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, List, Optional, Tuple

MODIFICADORES = ("ctrl", "alt", "shift", "win")

_APELIDOS = {
    "control": "ctrl", "ctl": "ctrl", "option": "alt", "opt": "alt", "super": "win",
    "meta": "win", "cmd": "win", "command": "win", "windows": "win",
    "return": "enter", "escape": "esc", "space": "espaco", "spacebar": "espaco",
    "del": "delete", "ins": "insert", "pgup": "pageup", "pgdn": "pagedown",
    "page_up": "pageup", "page_down": "pagedown", "up": "cima", "down": "baixo",
    "left": "esquerda", "right": "direita", "caps": "capslock", "print": "printscreen",
    "prtsc": "printscreen", "apps": "menu",
    "igual": "=", "mais": "=", "plus": "=", "equal": "=", "menos": "-", "minus": "-",
    "virgula": ",", "vírgula": ",", "comma": ",", "ponto": ".", "period": ".",
}

ESPECIAIS = frozenset({
    "enter", "tab", "esc", "espaco", "backspace", "delete", "insert", "home", "end",
    "pageup", "pagedown", "cima", "baixo", "esquerda", "direita", "capslock",
    "printscreen", "menu", *(f"f{n}" for n in range(1, 25)),
    # Pontuação com tecla própria em qualquer layout (o zoom é ctrl+= e ctrl+-).
    "=", "-", ",", ".",
})


class ErroDoComputador(RuntimeError):
    """O gesto não pôde ser feito: tecla desconhecida, fora da tela, sem tela."""


def canonica(tecla: str) -> str:
    nome = str(tecla or "").strip().lower()
    return _APELIDOS.get(nome, nome)


def ler_combinacao(texto: str) -> Tuple[Tuple[str, ...], str]:
    """`ctrl+Shift+T` → (("ctrl", "shift"), "t"). Os modificadores saem na
    ordem fixa; a tecla é uma só. `+` e `-` separam: com só
    `+`, `ctrl-alt-del` escaparia da trava."""
    import re

    partes = [canonica(p) for p in re.split(r"\s*[+]\s*|(?<=\w)-(?=\w)", str(texto or "").strip()) if p.strip()]
    if not partes:
        raise ErroDoComputador("tecla vazia")
    mods = tuple(m for m in MODIFICADORES if m in partes)
    resto = [p for p in partes if p not in MODIFICADORES]
    if len(resto) > 1:
        raise ErroDoComputador(f"uma tecla por vez com os modificadores: {texto!r}")
    tecla = resto[0] if resto else ""
    if not tecla:
        # So modificador (`shift`): vale como tecla, sem modificador junto.
        if len(mods) != 1:
            raise ErroDoComputador(f"combinação sem tecla: {texto!r}")
        return (), mods[0]
    if tecla not in ESPECIAIS and not (len(tecla) == 1 and tecla.isalnum() and tecla.isascii()):
        raise ErroDoComputador(f"tecla desconhecida: {tecla!r}")
    return mods, tecla


_EXIBIR = {
    "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "enter": "Enter", "tab": "Tab",
    "esc": "Esc", "espaco": "Espaço", "backspace": "Backspace", "delete": "Del", "insert": "Ins",
    "home": "Home", "end": "End", "pageup": "PgUp", "pagedown": "PgDn", "cima": "↑", "baixo": "↓",
    "esquerda": "←", "direita": "→", "capslock": "Caps", "printscreen": "PrtSc", "menu": "Menu",
}


def para_exibir(texto: str) -> str:
    """`ctrl+shift+t` → `Ctrl+Shift+T`, `cima` → `↑`: o que vai no balão e na
    teclinha do cartão."""
    mods, tecla = ler_combinacao(texto)
    return "+".join(_EXIBIR.get(p, p.upper()) for p in (*mods, tecla))


def conjunto(texto: str) -> FrozenSet[str]:
    """A combinação como conjunto, para comparar com a lista de travas."""
    mods, tecla = ler_combinacao(texto)
    return frozenset((*mods, tecla))


@dataclass(frozen=True)
class Tela:
    """Um print: PNG já encolhido para o modelo, e a escala de volta.

    `largura`/`altura` são do PNG; `largura_real`/`altura_real` da tela. A
    coordenada que o modelo devolve é do PNG e vira da tela por `para_tela`.
    """

    png: bytes
    largura: int
    altura: int
    largura_real: int
    altura_real: int

    def para_tela(self, x: float, y: float) -> Tuple[int, int]:
        fx = self.largura_real / max(1, self.largura)
        fy = self.altura_real / max(1, self.altura)
        return (min(self.largura_real - 1, max(0, round(x * fx))),
                min(self.altura_real - 1, max(0, round(y * fy))))

    @property
    def data_url(self) -> str:
        import base64

        return "data:image/png;base64," + base64.b64encode(self.png).decode("ascii")


# O lado maior do print que vai ao modelo: a tela inteira em
# resolução nativa custa token demais e não ajuda a achar o botão.
LADO_MAXIMO = 1456


def ampliar(imagem, largura_real: int, altura_real: int, lado_maximo: int = LADO_MAXIMO) -> Tela:
    """Amplia um recorte até 3x para leitura, ou reduz se passar do lado máximo."""
    from PIL import Image

    largura, altura = imagem.size
    fator = min(3.0, lado_maximo / max(1, largura, altura))
    if fator != 1.0:
        imagem = imagem.resize((max(1, round(largura * fator)), max(1, round(altura * fator))), Image.LANCZOS)
    saida = io.BytesIO()
    imagem.convert("RGB").save(saida, format="PNG", optimize=True)
    return Tela(saida.getvalue(), imagem.size[0], imagem.size[1], largura_real, altura_real)


def tela_de_imagem(imagem, lado_maximo: int = LADO_MAXIMO) -> Tela:
    """Pillow `Image` da tela inteira → `Tela` encolhida."""
    from PIL import Image

    largura_real, altura_real = imagem.size
    escala = min(1.0, lado_maximo / max(largura_real, altura_real))
    if escala < 1.0:
        imagem = imagem.resize((round(largura_real * escala), round(altura_real * escala)), Image.LANCZOS)
    saida = io.BytesIO()
    imagem.convert("RGB").save(saida, format="PNG", optimize=True)
    return Tela(saida.getvalue(), imagem.size[0], imagem.size[1], largura_real, altura_real)


RITMOS = frozenset({"automatico", "natural", "rapido", "instantaneo"})


def gravando() -> bool:
    """Com `TATO_GRAVAR=1` a moldura aparece em gravação de tela e no print do agente."""
    return os.environ.get("TATO_GRAVAR") == "1"


class BackendDoComputador(ABC):
    """Executa gestos síncronos em pixels da tela principal."""

    nome = "base"

    def __init__(self) -> None:
        # O que NÓS apertamos e ainda não soltamos: o `soltar_entrada` do
        # atalho solta só isso, e não as teclas que a pessoa está segurando.
        self._apertadas: set = set()
        self._trava_do_apertado = threading.Lock()

    def _apertou(self, item: str) -> None:
        with self._trava_do_apertado:
            self._apertadas.add(item)

    def _soltou(self, item: str) -> None:
        with self._trava_do_apertado:
            self._apertadas.discard(item)

    def apertadas(self) -> List[str]:
        with self._trava_do_apertado:
            return sorted(self._apertadas)

    # Leitura
    @abstractmethod
    def tela(self) -> Tela: ...

    @abstractmethod
    def tamanho(self) -> Tuple[int, int]: ...

    @abstractmethod
    def posicao_do_mouse(self) -> Tuple[int, int]: ...

    @abstractmethod
    def janelas(self) -> List[Dict[str, object]]:
        """[{titulo, em_foco, x, y, largura, altura}], as visíveis."""

    def programas(self, nome: str = "") -> Dict[str, object]:
        """Os programas abertos, com as janelas de cada um; com `nome`, também
        se ele roda sem janela e se está instalado."""
        raise ErroDoComputador("este sistema não lista os programas; use `janelas`")

    def abrir(self, nome: str, interromper: Callable[[], bool] = lambda: False) -> str:
        """Abre o programa instalado de nome `nome` e espera a janela dele. Devolve o que foi feito."""
        raise ErroDoComputador("este sistema não abre programa pelo nome; use o menu do sistema")

    def fechar_janela(self, titulo: str) -> str:
        """Pede para a janela com `titulo` fechar, como o X dela. Devolve o título."""
        raise ErroDoComputador("este sistema não fecha janela pelo título; clique no X dela")

    def focar(self, titulo: str) -> str:
        """Traz para a frente a janela aberta com `titulo` no título. Devolve o
        título inteiro dela."""
        raise ErroDoComputador("este sistema não traz janela para a frente; clique nela na barra de tarefas")

    def elementos(self) -> list:
        """Os `Elemento`s da janela em foco, pela acessibilidade do sistema
        (computador_elementos.py). Sem leitura, `ErroDoComputador`."""
        raise ErroDoComputador("este sistema não lê os elementos da tela; use a coordenada do print")

    def foco(self):
        """O `Elemento` com o foco do teclado, ou None quando não se sabe."""
        return None

    def invocar(self, alvo) -> str:
        """Aperta o `Elemento` pela acessibilidade, sem mover o mouse. Devolve
        o que foi feito."""
        raise ErroDoComputador("este sistema não age pela acessibilidade; use clicar")

    def focar_elemento(self, alvo) -> None:
        """Põe o foco do teclado no `Elemento` pela acessibilidade, sem mover
        o mouse (a janela dele vem para a frente)."""
        raise ErroDoComputador("este sistema não age pela acessibilidade; clique no campo e use digitar")

    def lista_editavel(self, alvo) -> bool:
        """Sem evidência de edição, a lista continua sendo seleção de opções."""
        return False

    def escolher_opcao(self, alvo, opcao: str) -> None:
        """Escolhe a opção da lista pelo nome, pela acessibilidade."""
        raise ErroDoComputador("este sistema não age pela acessibilidade; clique na lista e escolha")

    # Gestos
    @abstractmethod
    def mover(self, x: int, y: int) -> None: ...

    @abstractmethod
    def botao(self, botao: str, apertar: bool) -> None:
        """`esquerdo`, `direito` ou `meio`; apertar ou soltar."""

    @abstractmethod
    def tecla_bruta(self, tecla: str, apertar: bool) -> None:
        """Uma tecla canônica (ou modificador), apertar ou soltar."""

    @abstractmethod
    def escrever(self, texto: str) -> None:
        """Os caracteres como teclas apertadas e soltas, de uma vez, respeitando
        acento e símbolo. Quem digita é o `digitar`, que chama isto um
        caractere por vez."""

    @abstractmethod
    def roda(self, dx: int, dy: int) -> None:
        """Cliques de roda: dy > 0 desce, dx > 0 vai à direita."""

    def recorte(self, caixa: Tuple[int, int, int, int]) -> Tela:
        """Recorte `(x0, y0, x1, y1)` da tela, ampliado. Sem captura por região, recorta do print."""
        from PIL import Image

        inteira = self.tela()
        imagem = Image.open(io.BytesIO(inteira.png))
        fx = inteira.largura / max(1, inteira.largura_real)
        fy = inteira.altura / max(1, inteira.altura_real)
        x0, y0, x1, y1 = caixa
        pedaco = imagem.crop((round(x0 * fx), round(y0 * fy), max(round(x1 * fx), round(x0 * fx) + 1),
                              max(round(y1 * fy), round(y0 * fy) + 1)))
        return ampliar(pedaco, x1 - x0, y1 - y0)

    def texto(self, maximo: int = 12000) -> str:
        """O texto da janela da frente pela acessibilidade, até `maximo` caracteres."""
        raise ErroDoComputador("ler o texto da janela não existe neste sistema; use `ver`")

    def janelas_visiveis(self) -> Optional[set]:
        """Identificadores das janelas visíveis, ou None quando o sistema não diz."""
        return None

    def janela_da_frente(self) -> Optional[int]:
        """Identificador da janela que recebe o teclado, ou None."""
        return None

    def programa_da_frente(self) -> Optional[str]:
        """Executável da janela que recebe o teclado, ou None. Diálogo e pop-up contam como o mesmo programa."""
        return None

    # Os compostos, iguais para os dois sistemas.
    DESLIZAR_S = 0.25

    def deslizar(self, x: int, y: int) -> None:
        """Move o ponteiro gradualmente até a posição desejada.

        A configuração de movimento pode desativar a animação.
        """
        import os
        import time

        duracao = self.DESLIZAR_S if os.environ.get("TATO_COMPUTADOR_DESLIZAR", "1") != "0" else 0
        try:
            x0, y0 = self.posicao_do_mouse()
        except Exception:
            x0, y0 = x, y
        passos = max(1, round(duracao / 0.02)) if duracao and (x0, y0) != (x, y) else 1
        for i in range(1, passos + 1):
            # Reduz a velocidade ao se aproximar do destino.
            t = 1 - (1 - i / passos) ** 3
            self.mover(round(x0 + (x - x0) * t), round(y0 + (y - y0) * t))
            if i < passos:
                time.sleep(duracao / passos)

    # Intervalo entre teclas, e a pausa a mais depois de espaço e pontuação.
    DIGITAR_S = (0.035, 0.11)
    PAUSA_S = (0.06, 0.22)

    def digitar(self, texto: str, interromper: Callable[[], bool] = lambda: False,
                ritmo: str = "automatico", editores: frozenset = frozenset()) -> str:
        """Digita por teclas reais e devolve o ritmo usado.

        `natural` varia a pausa entre caracteres. `rapido` usa uma pausa fixa
        curta, que o editor consegue processar. `instantaneo` manda o texto
        inteiro de uma vez. `automatico` usa o rápido nos `editores` e o
        natural nos demais; TATO_COMPUTADOR_RITMO=0 também escolhe o rápido.
        """
        import os
        import random
        import time

        if ritmo not in RITMOS:
            raise ErroDoComputador("ritmo deve ser " + ", ".join(sorted(RITMOS)))
        frente = self.programa_da_frente()
        if ritmo == "automatico":
            executavel = str(frente or "").replace("\\", "/").rsplit("/", 1)[-1].lower()
            ritmo = ("rapido" if os.environ.get("TATO_COMPUTADOR_RITMO") == "0"
                     or executavel in editores else "natural")

        texto = str(texto)
        if ritmo == "instantaneo":
            if not interromper():
                self.escrever(texto)
            return ritmo
        # Outro programa veio para a frente no meio: o resto iria para ele.
        for i, caractere in enumerate(texto):
            if interromper():
                return ritmo
            if frente is not None and self.programa_da_frente() != frente:
                raise ErroDoComputador(
                    f"outro programa veio para a frente no meio da digitação: {i} de {len(texto)} caracteres "
                    "entraram, o resto não. Traga a janela certa com `focar`, confira e continue dali")
            self.escrever(caractere)
            if i == len(texto) - 1:
                continue
            if ritmo == "rapido":
                # Tab e Enter trocam de célula ou de campo: a planilha precisa de
                # tempo para gravar o valor antes da próxima tecla.
                pausa = 0.25 if caractere in "\n\t" else 0.05
            else:
                pausa = random.uniform(*self.DIGITAR_S)
                if caractere in " .,;:!?\n\t":
                    pausa += random.uniform(*self.PAUSA_S)
            time.sleep(pausa)
        return ritmo

    def clicar(self, x: int, y: int, botao: str = "esquerdo", vezes: int = 1) -> None:
        self.deslizar(x, y)
        for _ in range(max(1, min(vezes, 3))):
            self.botao(botao, True)
            self.botao(botao, False)

    def arrastar(self, de: Tuple[int, int], para: Tuple[int, int], botao: str = "esquerdo",
                 modificadores: Tuple[str, ...] = ()) -> None:
        """Arrasta com `botao` e `modificadores` segurados, como girar a vista em modelagem 3D."""
        apertados: List[str] = []
        try:
            for mod in modificadores:
                self.tecla_bruta(mod, True)
                apertados.append(mod)
            self.mover(*de)
            self.botao(botao, True)
            try:
                # Move em passos; um salto único não dispara o arraste em muitos programas.
                passos = 8
                for i in range(1, passos + 1):
                    self.mover(round(de[0] + (para[0] - de[0]) * i / passos),
                               round(de[1] + (para[1] - de[1]) * i / passos))
            finally:
                self.botao(botao, False)
        finally:
            for mod in reversed(apertados):
                self.tecla_bruta(mod, False)

    def sequencia(self, passos: List[Tuple[str, float]], pausa: float,
                  interromper: Callable[[], bool] = lambda: False) -> int:
        """Aperta as teclas em ordem, com `pausa` entre elas, e devolve quantas rodaram.

        Para no atalho ou se outro programa vier para a frente.
        """
        import time

        frente = self.programa_da_frente()
        for i, (combinacao, segurar) in enumerate(passos):
            if interromper():
                return i
            if frente is not None and self.programa_da_frente() != frente:
                raise ErroDoComputador(f"outro programa veio para a frente no meio da sequência: {i} de "
                                       f"{len(passos)} teclas rodaram. Traga a janela certa com `focar`")
            if segurar:
                self.segurar(combinacao, segurar, interromper)
            else:
                self.combinacao(combinacao)
            if pausa and i < len(passos) - 1:
                time.sleep(pausa)
        return len(passos)

    def combinacao(self, texto: str) -> None:
        mods, tecla = ler_combinacao(texto)
        apertados: List[str] = []
        try:
            for mod in mods:
                self.tecla_bruta(mod, True)
                apertados.append(mod)
            self.tecla_bruta(tecla, True)
            self.tecla_bruta(tecla, False)
        finally:
            for mod in reversed(apertados):
                self.tecla_bruta(mod, False)

    def segurar(self, texto: str, segundos: float, interromper: Callable[[], bool] = lambda: False) -> None:
        """A combinação apertada por `segundos` e solta no fim, sempre: jogo
        anda enquanto a tecla está embaixo, e o personagem não pode ficar
        andando depois de um erro. `interromper` (o atalho, o Assumir) solta
        antes da hora."""
        import time

        mods, tecla = ler_combinacao(texto)
        apertados: List[str] = []
        try:
            for item in (*mods, tecla):
                self.tecla_bruta(item, True)
                apertados.append(item)
            fim = time.monotonic() + max(0.0, segundos)
            while not interromper() and (falta := fim - time.monotonic()) > 0:
                time.sleep(min(0.05, falta))
        finally:
            for item in reversed(apertados):
                try:
                    self.tecla_bruta(item, False)
                except Exception:
                    pass

    # O lado da moldura (`computador_moldura.Backend`).
    def soltar_entrada(self) -> None:
        for item in self.apertadas():
            try:
                if item.startswith("botao:"):
                    self.botao(item.split(":", 1)[1], False)
                else:
                    self.tecla_bruta(item.split(":", 1)[1], False)
            except Exception:
                pass

    @abstractmethod
    def desenhar(self, quadro) -> None: ...

    @abstractmethod
    def ouvir_atalho(self, atalho: str, ao_apertar: Callable[[], None]) -> None: ...

    @abstractmethod
    def parar_de_ouvir(self) -> None: ...

    def fechar(self) -> None:
        """Libera o que o backend abriu (conexão, thread, janela)."""


def backend_do_sistema() -> BackendDoComputador:
    """O backend desta máquina, ou `ErroDoComputador` dizendo por que não há."""
    import os
    import sys

    if sys.platform.startswith("win"):
        from .computador_windows import BackendWindows

        return BackendWindows()
    if os.environ.get("DISPLAY"):
        from .computador_x11 import BackendX11

        return BackendX11(os.environ["DISPLAY"])
    raise ErroDoComputador(
        "sem tela nesta máquina: o computer use precisa de Windows ou de um Linux com X11 (DISPLAY)")


__all__ = ["BackendDoComputador", "ErroDoComputador", "ESPECIAIS", "LADO_MAXIMO", "MODIFICADORES", "Tela",
           "backend_do_sistema", "canonica", "conjunto", "ler_combinacao", "para_exibir", "tela_de_imagem"]
