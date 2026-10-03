"""Opera a tela, o mouse e o teclado sob autorização da pessoa.

Leituras não exigem aprovação; gestos exigem aprovação por turno.
O atalho de parada bloqueia gestos e encerra a moldura visível.
Elementos acessíveis são lidos em texto; `ver` fornece imagem quando necessário.
Campos de senha recusam digitação e dados pessoais exigem confirmação.
Uma sessão controla o computador por vez.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .computador_base import (
    LADO_MAXIMO, RITMOS, BackendDoComputador, ErroDoComputador, backend_do_sistema, conjunto, ler_combinacao,
)
from . import computador_vez
from .computador_elementos import desenhar, filtrar, para_o_modelo
from .computador_veredito import veredito
from .computador_moldura import Moldura, MolduraParada, atalho_de_parar

logger = logging.getLogger(__name__)

LEITURA = frozenset({"ver", "elementos", "janelas", "programas", "esperar", "ler"})
GESTOS = frozenset({"clicar", "clicar_duas", "clicar_direito", "mover", "arrastar", "rolar", "digitar", "tecla",
                    "invocar", "definir_valor", "focar", "abrir", "fechar"})
# Agem no controle pela acessibilidade: não mexem no mouse nem trazem a janela.
PELA_ACESSIBILIDADE = frozenset({"invocar", "definir_valor"})
ACOES = LEITURA | GESTOS | {"encerrar", "pedir_a_pessoa"}

# Nome errado não é consertado: é recusado, dizendo o certo.
_SUGESTOES = {
    "screenshot": "ver", "capture": "ver", "print": "ver", "olhar": "ver", "click": "clicar",
    "double_click": "clicar_duas", "right_click": "clicar_direito", "drag": "arrastar",
    "scroll": "rolar", "type": "digitar", "escrever": "digitar", "key": "tecla", "hotkey": "tecla",
    "atalho": "tecla", "wait": "esperar", "list_windows": "janelas", "move": "mover",
    "focus": "focar", "activate": "focar", "trazer": "focar", "close": "fechar", "open": "abrir", "launch": "abrir",
    "iniciar": "abrir", "processos": "programas", "apps": "programas", "call_user": "pedir_a_pessoa", "pedir_ajuda": "pedir_a_pessoa", "chamar_pessoa": "pedir_a_pessoa",
}

_TRAVAS_DE_TECLA = (
    frozenset({"win", "l"}),                      # bloquear a tela
    frozenset({"ctrl", "alt", "delete"}),         # tela de segurança
    frozenset({"ctrl", "alt", "backspace"}),      # derruba o X
    frozenset({"alt", "f4"}),                     # fecha a janela em foco
    frozenset({"ctrl", "shift", "delete"}),       # apagar sem lixeira em vários
    frozenset({"shift", "delete"}),               # apagar sem lixeira no Explorer
)

_TEXTO_PERIGOSO = [re.compile(p, re.IGNORECASE) for p in (
    r"curl\s+[^|]*\|\s*(ba)?sh", r"wget\s+[^|]*\|\s*(ba)?sh", r"\bsudo\s+rm\s+-[rf]",
    r":\s*\(\)\s*\{\s*:\|:\s*&\s*\}\s*;\s*:",
)]

_ESPERA_DEPOIS_DO_GESTO = 0.35  # a tela precisa reagir antes do print
# `tecla` com `segurar`: o bastante para andar num jogo, pouco para travar o turno.
_SEGURAR_MAXIMO = 10.0

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Os gestos que podem mirar um elemento numerado em vez de `x` e `y`.
_COM_ALVO = frozenset({"clicar", "clicar_duas", "clicar_direito", "mover", "rolar"})


@dataclass
class _Turno:
    """O que vale enquanto o turno de uma sessão usa o computador."""

    tarefa_id: int
    state: Any
    aprovado: bool = False
    moldura: Optional[Moldura] = None
    gestos: int = 0
    extras: Dict[str, Any] = field(default_factory=dict)
    # Marcado ao encerrar, no prazo estourado ou no cancelamento; o gesto para na próxima tecla.
    interrompido: threading.Event = field(default_factory=threading.Event)


_trava = threading.RLock()
_turnos: Dict[str, _Turno] = {}
_dono: Optional[str] = None  # a sessão com a moldura aberta
_sessao_dona: Optional[str] = None  # persiste entre turnos; um desktop, uma conversa
_backend: Optional[BackendDoComputador] = None


def _obter_backend() -> BackendDoComputador:
    global _backend
    with _trava:
        if _backend is None:
            _backend = backend_do_sistema()
        return _backend


def usar_backend(backend: Optional[BackendDoComputador]) -> None:
    """Troca o backend (teste, diagnóstico). None volta ao do sistema."""
    global _backend
    with _trava:
        _backend = backend


def escala(tamanho: Tuple[int, int]) -> float:
    """Tela → print: o mesmo corte do `tela_de_imagem`."""
    return min(1.0, LADO_MAXIMO / max(tamanho))


def _para_tela(x: Any, y: Any, tamanho: Tuple[int, int]) -> Tuple[int, int]:
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        raise ErroDoComputador("coordenada precisa de `x` e `y` numéricos, do print")
    fator = 1 / escala(tamanho)
    tx, ty = round(fx * fator), round(fy * fator)
    if not (0 <= tx < tamanho[0] and 0 <= ty < tamanho[1]):
        raise ErroDoComputador(f"({x}, {y}) fica fora do print")
    return tx, ty


def _para_print(x: int, y: int, tamanho: Tuple[int, int]) -> Tuple[int, int]:
    fator = escala(tamanho)
    return round(x * fator), round(y * fator)


def _combinacoes(argumentos: Dict[str, Any]) -> List[str]:
    """A `tecla` e cada passo da `sequencia`, sem o tempo segurado."""
    saida = [str(argumentos.get("tecla") or argumentos.get("teclas") or "")]
    for passo in argumentos.get("sequencia") or []:
        texto = passo.get("tecla") if isinstance(passo, dict) else passo
        saida.append(str(texto or "").rsplit(":", 1)[0] if ":" in str(texto or "") else str(texto or ""))
    return [c for c in saida if c.strip()]


_MAX_PASSOS, _MAX_SEGURADO_S = 40, 30.0


def _passos_da_sequencia(argumentos: Dict[str, Any]) -> List[Tuple[str, float]]:
    """Converte `sequencia` em pares (combinação, segundos segurados).

    Aceita "direita", "cima:0.6" ou {"tecla": "z", "segurar": 1}.
    """
    bruto = argumentos.get("sequencia")
    if not isinstance(bruto, list) or not bruto:
        raise ErroDoComputador("`sequencia` é uma lista de teclas, como [\"direita\", \"direita\", \"z\"]")
    if len(bruto) > _MAX_PASSOS:
        raise ErroDoComputador(f"no máximo {_MAX_PASSOS} teclas por sequência")
    passos: List[Tuple[str, float]] = []
    for item in bruto:
        if isinstance(item, dict):
            combinacao, segurar = str(item.get("tecla") or ""), item.get("segurar") or 0
        else:
            combinacao, _, segurar = str(item or "").partition(":")
        try:
            segundos = max(0.0, min(float(segurar or 0), _SEGURAR_MAXIMO))
        except (TypeError, ValueError):
            raise ErroDoComputador(f"tempo segurado inválido em «{item}»")
        ler_combinacao(combinacao)
        passos.append((combinacao, segundos))
    if sum(s for _, s in passos) > _MAX_SEGURADO_S:
        raise ErroDoComputador(f"a sequência segura teclas por mais de {_MAX_SEGURADO_S:g} s; divida em partes")
    return passos


_BOTOES = {"esquerdo": "esquerdo", "direito": "direito", "meio": "meio",
           "left": "esquerdo", "right": "direito", "middle": "meio"}


def _modificadores(texto: Any) -> Tuple[str, ...]:
    """Lê `com` ("shift", "ctrl+alt"); aceita só modificadores."""
    import re

    saida = []
    for parte in re.split(r"\s*\+\s*", str(texto or "").strip()):
        if not parte:
            continue
        tecla = ler_combinacao(parte)[1]
        if tecla not in ("ctrl", "shift", "alt"):
            raise ErroDoComputador("`com` aceita ctrl, shift e alt, juntos com +")
        saida.append(tecla)
    return tuple(saida)


def trava(acao: str, argumentos: Dict[str, Any]) -> Optional[str]:
    """O motivo de nunca fazer, ou None. Antes de qualquer aprovação."""
    for combinacao in (_combinacoes(argumentos) if acao == "tecla" else []):
        try:
            teclas = conjunto(combinacao)
        except ErroDoComputador:
            continue  # tecla inválida é erro de contrato, não trava
        if teclas == conjunto(atalho_de_parar()):
            return "esse é o atalho de parar do próprio Tato"
        for proibida in _TRAVAS_DE_TECLA:
            if proibida <= teclas:
                return f"a combinação {'+'.join(sorted(proibida))} derruba a sessão ou apaga sem volta"
    if acao == "digitar":
        texto = str(argumentos.get("texto") or "")
        for padrao in _TEXTO_PERIGOSO:
            if padrao.search(texto):
                return f"o texto casa com um comando perigoso ({padrao.pattern})"
        from ..politica import recusa_sem_apelacao

        motivo = recusa_sem_apelacao(texto, checar_alvo=False)
        if motivo:
            return f"o texto é um comando que o Tato nunca roda: {motivo}"
    return None


def descrever(acao: str, argumentos: Dict[str, Any]) -> str:
    """O que a pessoa lê no cartão e na moldura. `digitar` diz quanto, não o
    quê: senha na tela de quem estiver olhando é o que o navegador também evita."""
    if acao in ("clicar", "clicar_duas", "clicar_direito", "mover"):
        if argumentos.get("elemento") is not None:
            return f"{acao.replace('_', ' ')} no elemento {argumentos.get('elemento')}"
        return f"{acao.replace('_', ' ')} em ({argumentos.get('x')}, {argumentos.get('y')})"
    if acao == "arrastar":
        return (f"arrastar de ({argumentos.get('x')}, {argumentos.get('y')}) "
                f"a ({argumentos.get('para_x')}, {argumentos.get('para_y')})")
    if acao == "rolar":
        return f"rolar {argumentos.get('direcao') or 'baixo'} x{argumentos.get('quantidade') or 3}"
    if acao == "digitar":
        return f"digitar {len(str(argumentos.get('texto') or ''))} caracteres"
    if acao == "invocar":
        return f"apertar o elemento {argumentos.get('elemento')} sem mexer no mouse"
    if acao == "definir_valor":
        return f"preencher o elemento {argumentos.get('elemento')} com {len(str(argumentos.get('valor') or ''))} caracteres"
    if acao == "tecla" and isinstance(argumentos.get("sequencia"), list):
        return f"sequência de {len(argumentos['sequencia'])} teclas"
    if acao == "tecla":
        segurar = _segundos_de_segurar(argumentos)
        return (f"tecla {argumentos.get('tecla') or argumentos.get('teclas') or ''}"
                + (f" segurada {segurar:g} s" if segurar else ""))
    if acao == "focar":
        return f"trazer «{argumentos.get('janela') or ''}» para a frente"
    if acao == "fechar":
        return f"fechar a janela «{argumentos.get('janela') or ''}»"
    if acao == "abrir":
        return f"abrir o programa «{argumentos.get('nome') or ''}»"
    if acao == "programas":
        return "os programas abertos" + (f", procurando «{argumentos['nome']}»" if argumentos.get("nome") else "")
    if acao == "ver":
        return "verificando a tela e identificando os elementos" if argumentos.get("marcar") else "verificando a tela"
    if acao == "elementos":
        return "árvore de acessibilidade da janela em foco, sem print"
    if acao == "janelas":
        return "as janelas abertas"
    return acao


def _segundos_de_segurar(argumentos: Dict[str, Any]) -> float:
    """Quanto tempo a `tecla` fica apertada; 0 é apertar e soltar."""
    try:
        return max(0.0, min(float(argumentos.get("segurar") or 0), _SEGURAR_MAXIMO))
    except (TypeError, ValueError):
        return 0.0


def etiqueta_do_gesto(acao: str, argumentos: Dict[str, Any]) -> str:
    """O balão ao lado do mouse: o gesto em palavras, sem coordenada e sem o
    texto digitado."""
    if acao == "clicar":
        return "clicar"
    if acao == "clicar_duas":
        return "clicar duas vezes"
    if acao == "clicar_direito":
        return "clicar com o direito"
    if acao == "mover":
        return "mover o mouse"
    if acao == "arrastar":
        return "arrastar"
    if acao == "rolar":
        return f"rolar para {argumentos.get('direcao') or 'baixo'}"
    if acao == "digitar":
        return f"digitar · {len(str(argumentos.get('texto') or ''))} caracteres"
    if acao == "invocar":
        return "apertar · sem mouse"
    if acao == "definir_valor":
        return "preencher · sem teclado"
    if acao == "focar":
        return "trazer para a frente"
    if acao == "fechar":
        return "fechar janela"
    if acao == "abrir":
        return "abrir programa"
    if acao == "programas":
        return "listar programas"
    if acao == "ver":
        return "verificando a tela"
    if acao == "elementos":
        return "ler a janela"
    if acao == "janelas":
        return "listar janelas"
    if acao == "tecla" and isinstance(argumentos.get("sequencia"), list):
        return f"teclas · {len(argumentos['sequencia'])} em sequência"
    if acao == "tecla":
        from .computador_base import para_exibir

        segurar = _segundos_de_segurar(argumentos)
        try:
            return ("tecla · " + para_exibir(str(argumentos.get("tecla") or argumentos.get("teclas") or ""))
                    + (f" · {segurar:g} s" if segurar else ""))
        except ErroDoComputador:
            return "tecla"
    return acao


def _aviso_do_digitado(acao: str, argumentos: Dict[str, Any]) -> Dict[str, Any]:
    if acao != "digitar":
        return {}
    from ..segredos import ROTULOS, detectar

    achados = detectar(str(argumentos.get("texto") or ""))
    if not achados:
        return {}
    lista = ", ".join(ROTULOS[t] for t in achados)
    return {"dado_sensivel": achados,
            "aviso": f"foi digitado o que parece ser {lista}; avise a pessoa em uma frase, sem repetir o valor"}


def _autorizar(acao: str, argumentos: Dict[str, Any], sessao: str, turno: _Turno) -> Optional[Dict[str, Any]]:
    """None quando pode; senão o pedido do cartão, no formato do navegador.

    `na_hora=True`: "sempre" e "nesta conversa" não valem para o computador; o
    cartão só oferece "só desta vez", e "desta vez" aqui é o TURNO.
    """
    if turno.aprovado:
        return None
    from ..contrato import CodigoDeErro
    from ..politica import avaliar

    decisao = avaliar(f"computador {acao}", sessao, checar_alvo=False, na_hora=True)
    if decisao.get("permitido"):
        turno.aprovado = True
        return None
    base = {"ok": False, "executado": False, "fase": "permissao", "chave": "computador",
            "motivo": "usar o seu mouse e o seu teclado neste turno",
            "comando": f"computador {descrever(acao, argumentos)}"}
    if decisao.get("precisa_perguntar"):
        return {**base, "precisa_permissao": True, "so_desta_vez": True,
                "codigo_erro": CodigoDeErro.PRECISA_PERMISSAO}
    return {**base, "recusado": True, "motivo": str(decisao.get("motivo") or ""),
            "codigo_erro": CodigoDeErro.RECUSADO_POLITICA}


def _turno_de(sessao: str, state: Any) -> _Turno:
    tarefa = asyncio.current_task()
    tarefa_id = id(tarefa) if tarefa is not None else 0
    with _trava:
        atual = _turnos.get(sessao)
        if atual is not None and atual.tarefa_id == tarefa_id:
            return atual
        if atual is not None:
            _encerrar(sessao, atual)
        novo = _Turno(tarefa_id, state)
        _turnos[sessao] = novo
    if tarefa is not None:
        tarefa.add_done_callback(lambda _t: encerrar_turno(sessao, tarefa_id))
    return novo


def _reivindicar_sessao(sessao: str) -> Optional[str]:
    """Prende o desktop à conversa até fechamento explícito.

    A autorização continua curta (um turno); o vínculo com a tela não. Assim
    uma resposta intermediária não deixa outra conversa mover o mouse antes de
    a primeira retomar o trabalho.
    """
    global _sessao_dona
    with _trava:
        if _sessao_dona is None:
            _sessao_dona = sessao
            return None
        if _sessao_dona == sessao:
            return None
        return "outra conversa mantém uma sessão do computador; encerre-a antes de trocar"


def _parar_turno(sessao: str, loop: asyncio.AbstractEventLoop, tarefa: Optional[asyncio.Task]) -> None:
    """O `ao_parar` da moldura: cancela a chamada em curso."""
    if tarefa is not None and not tarefa.done():
        loop.call_soon_threadsafe(tarefa.cancel)


def _abrir_moldura(sessao: str, turno: _Turno, backend: BackendDoComputador) -> Optional[str]:
    """Abre a moldura do turno; o motivo de não poder, ou None."""
    global _dono
    with _trava:
        if turno.moldura is not None and turno.moldura.aberta:
            return None
        if turno.moldura is not None and turno.moldura.parada:
            return "parado pelo atalho"
        if _dono is not None and _dono != sessao:
            outro = _turnos.get(_dono)
            if outro is not None and outro.moldura is not None and outro.moldura.aberta:
                return "outra conversa está usando o computador agora; um mouse, uma conversa por vez"
        from . import computador_trava

        if (motivo := computador_trava.pegar()) is not None:
            return motivo
        # A task do turno, capturada aqui dentro dela: o atalho chega de outra
        # thread, onde perguntar "qual é a task atual" não faz sentido.
        loop, tarefa = asyncio.get_running_loop(), asyncio.current_task()
        moldura = Moldura(backend, ao_parar=lambda: _parar_turno(sessao, loop, tarefa),
                          contexto=_contexto(sessao, backend))
        try:
            moldura.abrir()
        except ErroDoComputador as exc:
            computador_trava.soltar()
            return str(exc)
        turno.moldura = moldura
        _dono = sessao
    # A fala do passo que está rodando já saiu antes da ferramenta: a moldura
    # abre com ela, e as seguintes chegam por `ouvir_fala`.
    passos = getattr(turno.state, "processo_do_turno", None) or []
    for passo in reversed(passos):
        if passo.get("fala"):
            moldura.falar(passo["fala"])
            break
    return None


def _contexto(sessao: str, backend: BackendDoComputador) -> str:
    """A janela em foco, para a linha de cima da caixinha. Gravando, fica em
    branco: o título do chat costuma ter o nome do projeto."""
    from .computador_base import gravando

    if gravando():
        return ""
    try:
        janela = next((j for j in backend.janelas() if j.get("em_foco")), None)
    except Exception:
        janela = None
    titulo = str((janela or {}).get("titulo") or "")
    return titulo if len(titulo) <= 34 else titulo[:33] + "…"


def ouvir_fala(state: Any, evento: Dict[str, Any]) -> None:
    """O registro do processo chama a cada `tool_start`: a fala vai à moldura
    da sessão, se houver uma aberta. Barato quando não há."""
    sessao = str(getattr(state, "session_id", "") or "")
    with _trava:
        turno = _turnos.get(sessao)
        moldura = turno.moldura if turno is not None else None
    if moldura is not None:
        moldura.ouvir(evento)


def _passo_sem_fala(state: Any) -> bool:
    """O passo que está rodando (o último do processo) veio sem fala do modelo."""
    passos = getattr(state, "processo_do_turno", None) or []
    ultimo = passos[-1] if passos else None
    return not (isinstance(ultimo, dict) and ultimo.get("fala"))


def _anunciar(turno: _Turno, acao: str, argumentos: Dict[str, Any]) -> None:
    """Emite o gesto e escreve na moldura o `dizer` ou, sem ele, a descrição do gesto."""
    _emitir_gesto(turno, acao, argumentos, "rodando")
    if turno.moldura is None:
        return
    if _dizer(argumentos):
        turno.extras["disse"] = True
        turno.moldura.falar(_dizer(argumentos))
    elif _passo_sem_fala(turno.state) and not turno.extras.get("disse"):
        # Depois de um `dizer`, a caixinha guarda só as frases do agente.
        turno.moldura.falar(descrever(acao, argumentos))


def _dizer(argumentos: Dict[str, Any]) -> str:
    return " ".join(str(argumentos.get("dizer") or "").split())


def _dizer_na_moldura(sessao: str, argumentos: Dict[str, Any]) -> None:
    """Escreve o `dizer` na moldura aberta, para ações que não passam por `_anunciar`."""
    if not _dizer(argumentos):
        return
    with _trava:
        turno = _turnos.get(sessao)
        moldura = turno.moldura if turno is not None else None
    if moldura is not None:
        turno.extras["disse"] = True
        moldura.falar(_dizer(argumentos))


def encerrar_do_turno(state: Any) -> None:
    """Encerra o turno, revoga a aprovação e descarta a captura anterior.

    A sessão permanece disponível para uma próxima chamada autorizada.
    """
    if getattr(state, "print_da_tela", None):
        state.print_da_tela = None
    sessao = str(getattr(state, "session_id", "") or "")
    with _trava:
        tem_turno = sessao in _turnos
    if tem_turno:
        encerrar_turno(sessao)
        _emitir("sessao", status="pronta")


def encerrar_turno(sessao: str, tarefa_id: Optional[int] = None) -> None:
    """Fim do turno (a task acabou, deu erro ou foi cancelada): fecha a
    moldura, esquece a aprovação e o print."""
    with _trava:
        turno = _turnos.get(sessao)
        if turno is None or (tarefa_id is not None and turno.tarefa_id != tarefa_id):
            return
        _encerrar(sessao, turno)


def _encerrar(sessao: str, turno: _Turno) -> None:
    global _dono
    turno.interrompido.set()
    _turnos.pop(sessao, None)
    if _dono == sessao:
        _dono = None
        from . import computador_trava

        computador_trava.soltar()
    if turno.moldura is not None:
        try:
            turno.moldura.fechar()
        except Exception:
            logger.warning("Não consegui fechar a moldura.", exc_info=True)
    try:
        turno.state.print_da_tela = None
    except Exception:
        pass


def encerrar_sessao(sessao: str) -> bool:
    """Libera explicitamente a sessão lógica e qualquer moldura ativa."""
    global _sessao_dona
    with _trava:
        existia = _sessao_dona == sessao or sessao in _turnos
        turno = _turnos.get(sessao)
        if turno is not None:
            _encerrar(sessao, turno)
        if _sessao_dona == sessao:
            _sessao_dona = None
    return existia


def sessao_ativa(sessao: str) -> bool:
    """Consulta o vínculo sem tocar na tela nem renovar autorização."""
    with _trava:
        return _sessao_dona == sessao


def encerrar_todos() -> None:
    """Shutdown: fecha turnos, solta o desktop e encerra o backend."""
    global _backend, _sessao_dona
    with _trava:
        for sessao, turno in list(_turnos.items()):
            _encerrar(sessao, turno)
        _turnos.clear()
        _sessao_dona = None
        backend, _backend = _backend, None
    if backend is not None:
        try:
            backend.fechar()
        except Exception:
            logger.warning("Não consegui fechar o backend do computador.", exc_info=True)


def _tirar_moldura(sessao: str) -> None:
    """Fecha a moldura sem soltar a sessão: a tela fica livre para a pessoa, e
    o print que o modelo ia ver é esquecido."""
    global _dono
    with _trava:
        turno = _turnos.get(sessao)
        moldura = turno.moldura if turno is not None else None
        if turno is not None:
            turno.moldura = None
            try:
                turno.state.print_da_tela = None
            except Exception:
                pass
        if _dono == sessao:
            _dono = None
    if moldura is not None:
        try:
            moldura.fechar()
        except Exception:
            logger.warning("Não consegui fechar a moldura.", exc_info=True)


def parar_turno(sessao: str) -> bool:
    """O atalho de parar, pedido de fora (o cliente MCP cancelou a chamada):
    nenhum gesto roda depois, teclas e botões soltam, a moldura sai."""
    with _trava:
        turno = _turnos.get(sessao)
        moldura = turno.moldura if turno is not None else None
    if moldura is None:
        return False
    moldura.parar()
    return True


def assumir(sessao: str) -> Dict[str, Any]:
    """O botão Assumir: a vez vai para a pessoa e o turno que usa o computador
    para. Vale para quem estiver com a moldura, mesmo em outra conversa."""
    vez = computador_vez.passar_para_pessoa(sessao, "Você assumiu o computador")
    with _trava:
        molduras = [t.moldura for t in _turnos.values() if t.moldura is not None]
        sessoes = set(_turnos) | {sessao}
    # O mesmo do atalho: fecha o portão dos gestos, solta tecla e botão que o
    # agente deixou apertados, para o turno e tira a moldura.
    for moldura in molduras:
        moldura.parar()
    for alvo in sessoes:
        _tirar_moldura(alvo)
    return computador_vez.publica(vez)


def devolver() -> Dict[str, Any]:
    """O botão Devolver: a vez volta para o agente."""
    return computador_vez.publica(computador_vez.devolver())


def _recusa_da_vez() -> Optional[Dict[str, Any]]:
    vez = computador_vez.atual()
    if not vez.da_pessoa:
        return None
    motivo = f" ({vez.motivo})" if vez.motivo else ""
    return {"ok": False, "executado": False, "pessoa_no_controle": True,
            "erro": f"a vez é da pessoa{motivo}: nada de ver nem mexer no computador até ela devolver",
            "proximo": ("encerre o turno dizendo em uma frase o que falta ela fazer; ela devolve pelo "
                        "botão Devolver do cartão do computador")}


def _anulado(turno: _Turno, acao: str, argumentos: Dict[str, Any]) -> Dict[str, Any]:
    """A vez trocou no meio do gesto: o print de depois é da pessoa, não do agente."""
    try:
        turno.state.print_da_tela = None
    except Exception:
        pass
    if acao in GESTOS:
        _emitir_gesto(turno, acao, argumentos, "parado")
    return {"ok": False, "executado": acao in GESTOS, "anulado": True, "pessoa_no_controle": True,
            "erro": "a pessoa assumiu o computador no meio da ação; o que veio depois foi descartado"}


def _pedir_a_pessoa(argumentos: Dict[str, Any], sessao: str) -> Dict[str, Any]:
    motivo = " ".join(str(argumentos.get("motivo") or "").split())
    if not motivo:
        return {"erro": "pedir_a_pessoa precisa de `motivo`: o passo que é dela (ex.: fazer login no portal)"}
    if (erro := _reivindicar_sessao(sessao)) is not None:
        return {"ok": False, "executado": False, "erro": erro}
    vez = computador_vez.passar_para_pessoa(sessao, motivo)
    _tirar_moldura(sessao)
    _emitir("vez", com=vez.com, motivo=vez.motivo)
    return {"ok": True, "executado": True, "acao": "pedir_a_pessoa", "vez": vez.com, "motivo": vez.motivo,
            "proximo": ("encerre o turno agora: diga em uma frase o que ela precisa fazer e que, ao terminar, "
                        "clique em Devolver no cartão do computador. Até lá o computador recusa tudo, até o print.")}


def _guardar_print(state: Any, tela, turno: Optional[_Turno] = None) -> None:
    state.print_da_tela = {"data_url": tela.data_url, "largura": tela.largura, "altura": tela.altura}
    if turno is not None:
        # O print inteiro é a referência das coordenadas que o agente vai mandar.
        # Recorte ampliado (menor que a tela) não substitui o print inteiro.
        ref = turno.extras.get("print_ref")
        if getattr(tela, "largura_real", None) is not None and (ref is None or tela.largura_real >= ref[1]):
            turno.extras["print_ref"] = (tela.png, tela.largura_real, tela.altura_real)
            turno.extras["gestos_desde_o_print"] = 0
        # "N prints neste turno", no rodapé do cartão.
        turno.extras["prints"] = int(turno.extras.get("prints") or 0) + 1


def _emitir(fase: str, **dados: Any) -> None:
    from ..emissor import emitir

    emitir({"type": "computador", "fase": fase, **dados})


def _fala_atual(state: Any) -> str:
    for passo in reversed(getattr(state, "processo_do_turno", None) or []):
        if isinstance(passo, dict) and passo.get("fala"):
            return str(passo["fala"])
    return ""


def _cabecalho(sessao: str, backend: BackendDoComputador) -> Dict[str, Any]:
    """Retorna a janela em foco e os dados do objetivo ativo."""
    dados: Dict[str, Any] = {}
    try:
        janela = next((j for j in backend.janelas() if j.get("em_foco")), None)
        if janela and janela.get("titulo"):
            dados["janela"] = str(janela["titulo"])[:60]
    except Exception:
        pass
    return dados


def _emitir_gesto(turno: _Turno, acao: str, argumentos: Dict[str, Any], status: str) -> None:
    from .computador_base import para_exibir

    extra: Dict[str, Any] = {}
    if acao == "tecla":
        try:
            extra["tecla"] = para_exibir(str(argumentos.get("tecla") or argumentos.get("teclas") or ""))
        except ErroDoComputador:
            pass
    _emitir("gesto", acao=acao, status=status, titulo=etiqueta_do_gesto(acao, argumentos),
            descricao=descrever(acao, argumentos), fala=_fala_atual(turno.state),
            prints=int(turno.extras.get("prints") or 0), leituras=int(turno.extras.get("leituras") or 0),
            **turno.extras.get("cabecalho", {}), **extra)


def _executar_gesto(backend: BackendDoComputador, acao: str, argumentos: Dict[str, Any], tamanho,
                    interromper: Callable[[], bool] = lambda: False) -> None:
    if acao in ("clicar", "clicar_duas", "clicar_direito"):
        x, y = _para_tela(argumentos.get("x"), argumentos.get("y"), tamanho)
        backend.clicar(x, y, "direito" if acao == "clicar_direito" else "esquerdo",
                       2 if acao == "clicar_duas" else 1)
    elif acao == "mover":
        backend.mover(*_para_tela(argumentos.get("x"), argumentos.get("y"), tamanho))
    elif acao == "arrastar":
        backend.arrastar(_para_tela(argumentos.get("x"), argumentos.get("y"), tamanho),
                         _para_tela(argumentos.get("para_x"), argumentos.get("para_y"), tamanho),
                         _BOTOES[str(argumentos.get("botao") or "esquerdo").lower()],
                         _modificadores(argumentos.get("com")))
    elif acao == "rolar":
        if argumentos.get("x") is not None:
            backend.mover(*_para_tela(argumentos.get("x"), argumentos.get("y"), tamanho))
        quantidade = max(1, min(int(argumentos.get("quantidade") or 3), 20))
        direcao = str(argumentos.get("direcao") or "baixo").lower()
        dx, dy = {"baixo": (0, 1), "cima": (0, -1), "direita": (1, 0), "esquerda": (-1, 0),
                  "down": (0, 1), "up": (0, -1), "right": (1, 0), "left": (-1, 0)}.get(direcao, (0, 1))
        mods = _modificadores(argumentos.get("com"))
        # O modificador é solto mesmo se a rolagem falhar.
        for mod in mods:
            backend.tecla_bruta(mod, True)
        try:
            backend.roda(dx * quantidade, dy * quantidade)
        finally:
            for mod in reversed(mods):
                backend.tecla_bruta(mod, False)
    elif acao == "invocar":
        argumentos["_feito"] = backend.invocar(argumentos["_alvo"])
    elif acao == "definir_valor":
        alvo, valor = argumentos["_alvo"], str(argumentos.get("valor") or "")
        if alvo.papel == "lista":
            backend.escolher_opcao(alvo, valor)
            return
        # Texto é digitado, nunca posto direto no controle: o foco no campo
        # pela acessibilidade, tudo selecionado, e o valor tecla por tecla.
        backend.focar_elemento(alvo)
        focado = backend.foco()
        if focado is not None and focado.papel == "senha":
            raise ErroDoComputador("o foco caiu num campo de senha; senha não passa pelo agente")
        backend.combinacao("ctrl+a")
        if valor:
            backend.digitar(valor, interromper)
        else:
            backend.combinacao("backspace")
    elif acao == "focar":
        argumentos["_feito"] = backend.focar(str(argumentos.get("janela") or ""))
    elif acao == "fechar":
        argumentos["_feito"] = "pedido para fechar «" + backend.fechar_janela(str(argumentos.get("janela") or "")) + "»"
    elif acao == "abrir":
        argumentos["_feito"] = backend.abrir(str(argumentos.get("nome") or ""), interromper)
    elif acao == "digitar":
        texto = str(argumentos.get("texto") or "")
        if not texto:
            raise ErroDoComputador("digitar precisa de `texto`")
        ritmo = backend.digitar(texto, interromper, str(argumentos.get("ritmo") or "automatico"),
                                _editores_rapidos())
        argumentos["_feito"] = f"ritmo {ritmo}"
    elif acao == "tecla" and argumentos.get("sequencia"):
        passos = _passos_da_sequencia(argumentos)
        try:
            pausa = max(0.0, min(float(argumentos.get("pausa") or 0.15), 2.0))
        except (TypeError, ValueError):
            pausa = 0.15
        feitas = backend.sequencia(passos, pausa, interromper)
        argumentos["_feito"] = f"{feitas} de {len(passos)} teclas"
    elif acao == "tecla":
        combinacao = str(argumentos.get("tecla") or argumentos.get("teclas") or "")
        ler_combinacao(combinacao)  # valida antes de apertar qualquer coisa
        if segurar := _segundos_de_segurar(argumentos):
            backend.segurar(combinacao, segurar, interromper)
        else:
            backend.combinacao(combinacao)


def _editores_rapidos() -> frozenset:
    """Os programas em que o ritmo automático digita rápido, declarados em ferramentas.json."""
    import json
    from pathlib import Path

    try:
        ferramentas = json.loads((Path(__file__).resolve().parent.parent / "ferramentas.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    meta = next((f for f in ferramentas if f.get("name") == "computador"), {})
    return frozenset(str(n).lower() for n in meta.get("editores_rapidos") or [])


def _contrato(acao: str, argumentos: Dict[str, Any]) -> Optional[str]:
    if acao in PELA_ACESSIBILIDADE:
        try:
            int(argumentos.get("elemento"))
        except (TypeError, ValueError):
            return f"{acao} precisa de `elemento`, o número da última leitura"
        if acao == "definir_valor" and argumentos.get("valor") is None:
            return "definir_valor precisa de `valor`"
        return None
    if acao in _COM_ALVO and argumentos.get("elemento") is not None:
        try:
            int(argumentos["elemento"])
        except (TypeError, ValueError):
            return "`elemento` é o número que aparece no print marcado"
        return None
    if acao in ("clicar", "clicar_duas", "clicar_direito", "mover", "arrastar"):
        if argumentos.get("x") is None or argumentos.get("y") is None:
            return f"{acao} precisa de `x` e `y`, a coordenada no print, ou de `elemento`"
    if acao == "arrastar" and (argumentos.get("para_x") is None or argumentos.get("para_y") is None):
        return "arrastar precisa de `para_x` e `para_y`"
    if acao == "digitar" and not str(argumentos.get("texto") or ""):
        return "digitar precisa de `texto`"
    if acao == "digitar" and str(argumentos.get("ritmo") or "automatico") not in RITMOS:
        return "ritmo deve ser " + ", ".join(sorted(RITMOS))
    if acao in ("focar", "fechar") and not str(argumentos.get("janela") or "").strip():
        return f"{acao} precisa de `janela`, parte do título como aparece em `janelas`"
    if acao == "abrir" and not str(argumentos.get("nome") or "").strip():
        return "abrir precisa de `nome`, o nome do programa como aparece no menu Iniciar"
    if acao == "tecla":
        try:
            if argumentos.get("sequencia"):
                _passos_da_sequencia(argumentos)
            else:
                ler_combinacao(str(argumentos.get("tecla") or argumentos.get("teclas") or ""))
        except ErroDoComputador as exc:
            return str(exc)
    if acao in ("arrastar", "rolar"):
        if acao == "arrastar" and str(argumentos.get("botao") or "esquerdo").lower() not in _BOTOES:
            return "`botao` é esquerdo, direito ou meio"
        try:
            _modificadores(argumentos.get("com"))
        except ErroDoComputador as exc:
            return str(exc)
    return None


def _numerar(turno: _Turno, achados: list) -> None:
    """Os números da última leitura valem para `elemento: n` até a próxima;
    a leitura inteira fica para o veredito do gesto seguinte."""
    turno.extras["marcas"] = {n: e for n, e in enumerate(achados, 1)}
    turno.extras["vistos"] = list(achados)


# Janelas que já tiveram a árvore pedida: o Chromium (Chrome, VS Code, apps
# Electron) liga a acessibilidade na primeira consulta de quem pergunta, e essa
# primeira leitura vem só com os botões da moldura da janela.
_ACORDADAS: set = set()
_ARVORE_MAGRA = 5
_ESPERA_DA_ARVORE_S = 0.7


def _janela_em_foco(elementos: list) -> str:
    """A primeira janela que não é o menu aberto nem a barra de tarefas: a em foco."""
    return next((e.janela for e in elementos if e.janela and e.janela not in ("menu", "barra de tarefas")), "")


async def _ler_elementos(backend: BackendDoComputador, tamanho: Tuple[int, int]) -> Tuple[list, Optional[str]]:
    """Os elementos filtrados, ou [] e o aviso de por que não há."""
    try:
        brutos = await asyncio.to_thread(backend.elementos)
    except ErroDoComputador as exc:
        return [], f"{exc}; use a coordenada do print"
    em_foco = _janela_em_foco(brutos)
    if em_foco and em_foco not in _ACORDADAS:
        # Uma vez por janela: jogo e canvas vêm magros sempre, e pagam a espera uma vez só.
        _ACORDADAS.add(em_foco)
        if sum(1 for e in brutos if e.janela == em_foco) <= _ARVORE_MAGRA:
            await asyncio.sleep(_ESPERA_DA_ARVORE_S)
            try:
                de_novo = await asyncio.to_thread(backend.elementos)
            except ErroDoComputador:
                de_novo = []
            if len(de_novo) > len(brutos):
                brutos = de_novo
    achados = filtrar(brutos, tamanho)
    if not achados:
        return [], "a janela não descreve os elementos; use a coordenada do print"
    return achados, None


async def _tirar_print(backend: BackendDoComputador, turno: _Turno, tamanho: Tuple[int, int],
                       marcar: bool) -> Tuple[Any, Dict[str, Any]]:
    """O print; com `marcar`, um número desenhado sobre cada elemento."""
    tela = await asyncio.to_thread(backend.tela)
    if not marcar:
        return tela, {}
    achados, aviso = await _ler_elementos(backend, tamanho)
    _numerar(turno, achados)
    if aviso:
        return tela, {"elementos_aviso": aviso}
    tela = await asyncio.to_thread(desenhar, tela, achados)
    return tela, {"elementos": para_o_modelo(achados, escala(tamanho))}


async def _conferir_por_texto(backend: BackendDoComputador, turno: _Turno, tamanho: Tuple[int, int],
                              acao: str, argumentos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Depois do gesto, a lista de elementos em vez do print, com o veredito:
    custa texto, não imagem. None quando a janela não se descreve (aí vai o
    print)."""
    achados, _aviso = await _ler_elementos(backend, tamanho)
    if not achados:
        return None
    antes = turno.extras.get("vistos")
    julgado = veredito(acao, argumentos, argumentos.get("_alvo"), antes, achados)
    if julgado.get("veredito") == "sem_efeito_aparente":
        # Janela que abre ou campo que muda um pouco depois do gesto: lê de novo uma vez.
        await asyncio.sleep(_ESPERA_DEPOIS_DO_GESTO * 2)
        outros, _aviso = await _ler_elementos(backend, tamanho)
        if outros:
            segundo = veredito(acao, argumentos, argumentos.get("_alvo"), antes, outros)
            if segundo.get("veredito") != "sem_efeito_aparente":
                achados, julgado = outros, segundo
    _numerar(turno, achados)
    turno.extras["leituras"] = int(turno.extras.get("leituras") or 0) + 1
    return {"elementos": para_o_modelo(achados, escala(tamanho)), **julgado}


async def _entregar_print(state: Any, tela, turno: _Turno, marcas: Dict[str, Any]) -> Dict[str, Any]:
    """Guarda a captura: ela vai como imagem na resposta desta chamada."""
    _guardar_print(state, tela, turno)
    return {}


def _sem_trechos(procurar: str) -> str:
    nota = f"«{procurar}» não aparece no texto da janela; confira com `ver`"
    if "r$" in procurar.lower():
        # O navegador entrega preço por extenso para a acessibilidade.
        nota += ". Preço costuma vir por extenso no texto (\"595 reais com 71 centavos\"): procure «reais»"
    return nota


def _mesmo_print(turno: _Turno, tela) -> bool:
    """O print saiu idêntico ao último deste turno? Guarda o de agora."""
    import hashlib

    digest = hashlib.sha256(tela.png).hexdigest()
    igual = turno.extras.get("ultimo_print") == digest
    turno.extras["ultimo_print"] = digest
    return igual


_CONFERE_O_ALVO = frozenset({"clicar", "clicar_duas", "clicar_direito", "arrastar"})


async def _alvo_mudou(backend: BackendDoComputador, turno: _Turno, acao: str, argumentos: Dict[str, Any],
                      tamanho: Tuple[int, int]) -> Optional[Dict[str, Any]]:
    """Clique por coordenada depois de outro gesto: a tela em volta do alvo ainda
    é a do print? Um diálogo que fechou ou mudou de lugar faz a coordenada cair
    em outra coisa."""
    if acao not in _CONFERE_O_ALVO or argumentos.get("elemento") is not None or argumentos.get("confiar_no_print"):
        return None
    ref = turno.extras.get("print_ref")
    if ref is None or not turno.extras.get("gestos_desde_o_print") or tuple(ref[1:]) != tuple(tamanho):
        return None
    try:
        x, y = _para_tela(argumentos.get("x"), argumentos.get("y"), tamanho)
        mudou = await asyncio.to_thread(_fracao_mudada, backend, ref, (x, y), tamanho)
    except Exception:
        return None
    if mudou < 0.25:
        return None
    gestos = int(turno.extras.get("gestos_desde_o_print") or 0)
    return {"ok": False, "executado": False, "recusado": True, "alvo_mudou": round(mudou, 2),
            "erro": (f"a tela em volta de ({argumentos.get('x')}, {argumentos.get('y')}) mudou desde o último "
                     f"print ({gestos} gesto(s) depois dele): a coordenada pode cair em outra coisa"),
            "proximo": ("confira com `ver` ou `elementos` e mire de novo. Se a mudança é a esperada, como "
                        "pintar sobre o que você acabou de desenhar, repita com `confiar_no_print: true`")}


def _fracao_mudada(backend: BackendDoComputador, ref, ponto: Tuple[int, int], tamanho: Tuple[int, int]) -> float:
    """Fração de pontos que mudou num quadrado em volta de `ponto`, print contra tela agora."""
    import io

    from PIL import Image, ImageChops

    raio = 80  # pixels da tela
    caixa = (max(0, ponto[0] - raio), max(0, ponto[1] - raio),
             min(tamanho[0], ponto[0] + raio), min(tamanho[1], ponto[1] + raio))
    impresso = Image.open(io.BytesIO(ref[0]))
    fator = impresso.width / max(1, tamanho[0])
    antes = impresso.crop(tuple(round(v * fator) for v in caixa)).convert("L").resize((40, 40))
    agora = Image.open(io.BytesIO(backend.recorte(caixa).png)).convert("L").resize((40, 40))
    # Limiar baixo: cinza-claro que vira branco (fundo que virou card) também conta.
    diferenca = ImageChops.difference(antes, agora).point(lambda p: 255 if p > 12 else 0)
    return diferenca.histogram()[255] / 1600


async def _estabilizar(backend: BackendDoComputador, turno: _Turno, tamanho: Tuple[int, int]) -> None:
    """Logo depois de um gesto, espera a tela parar de mudar antes do print (até ~1 s)."""
    if time.monotonic() - float(turno.extras.get("ultimo_gesto_em") or 0) > 2.0:
        return
    caixa = (0, 0, tamanho[0], tamanho[1])
    try:
        anterior = await asyncio.to_thread(_miniatura, backend, caixa)
        for _ in range(6):
            await asyncio.sleep(0.15)
            agora = await asyncio.to_thread(_miniatura, backend, caixa)
            if not _diferentes(anterior, agora):
                return
            anterior = agora
    except Exception:
        return


_NAVEGADORES = frozenset({"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
                          "google-chrome", "chromium", "firefox"})


async def _ler_texto(backend: BackendDoComputador, maximo: int, procurar: str) -> str:
    """O texto da janela. Navegador que acabou de carregar ainda não montou a
    página para a acessibilidade: veio quase nada, lê de novo uma vez."""
    texto = await asyncio.to_thread(backend.texto, maximo)
    pouco = len(texto.strip()) < 300 or (procurar and procurar.lower() not in texto.lower())
    if not pouco:
        return texto
    try:
        frente = str(await asyncio.to_thread(backend.programa_da_frente) or "").replace("\\", "/").rsplit("/", 1)[-1]
    except Exception:
        frente = ""
    if frente.lower() not in _NAVEGADORES:
        return texto
    for _ in range(3):
        await asyncio.sleep(1.0)
        texto = await asyncio.to_thread(backend.texto, maximo)
        if len(texto.strip()) >= 300 and (not procurar or procurar.lower() in texto.lower()):
            break
    return texto


def _mirar(argumentos: Dict[str, Any], turno: _Turno, tamanho: Tuple[int, int]) -> Optional[str]:
    """`elemento: n` vira o centro dele, no espaço do print. O erro, ou None."""
    if argumentos.get("elemento") is None:
        return None
    numero = int(argumentos["elemento"])
    alvo = (turno.extras.get("marcas") or {}).get(numero)
    if alvo is None:
        return (f"não há elemento {numero} na última leitura; peça `elementos` (ou `ver` com `marcar`) "
                "e use um número dela")
    argumentos["_alvo"] = alvo
    argumentos["x"], argumentos["y"] = _para_print(*alvo.centro, tamanho)
    return None


def _recusa_de_senha(acao: str, argumentos: Dict[str, Any], alvo) -> Optional[Dict[str, Any]]:
    """Senha não passa pelo teclado do agente. O que ele digita sai do modelo,
    e senha no pedido à API do provedor pode custar a conta da pessoa; o
    caminho é o preenchimento automático.
    Letra solta pela `tecla` no mesmo campo seria o mesmo contorno; Enter, Tab
    e colar continuam valendo."""
    if alvo is None or alvo.papel != "senha":
        return None
    if acao == "definir_valor":
        pass
    elif acao == "tecla":
        modificadores, tecla = ler_combinacao(str(argumentos.get("tecla") or argumentos.get("teclas") or ""))
        if len(tecla) != 1 or set(modificadores) - {"shift"}:
            return None
    elif acao != "digitar":
        return None
    return {"ok": False, "executado": False, "recusado": True, "sem_apelacao": True, "campo_de_senha": True,
            "erro": f"o foco está no campo de senha «{alvo.nome or 'senha'}»: senha não passa pelo teclado do agente",
            "proximo": ("use o preenchimento automático: clique no campo e escolha a senha salva que o "
                        "navegador ou o gerenciador de senhas oferecer; sem senha salva, chame "
                        "`pedir_a_pessoa` com o motivo")}


def _sensivel(argumentos: Dict[str, Any], alvo) -> Optional[str]:
    """O que torna este `digitar` sensível, em palavras, ou None: campo de
    e-mail ou texto que parece dado pessoal."""
    if alvo is not None and alvo.papel == "campo" and "mail" in alvo.nome.lower():
        return f"um e-mail no campo «{alvo.nome}»"
    from ..segredos import ROTULOS, detectar

    texto = str(argumentos.get("texto") or "")
    # E-mail fica fora do aviso geral (seria aviso demais na conversa), mas
    # digitar e-mail num formulário é entregar um dado: aqui pergunta.
    rotulos = [ROTULOS[t] for t in detectar(texto)] + (["e-mail"] if _EMAIL.search(texto) else [])
    if rotulos:
        return "o que parece ser " + ", ".join(rotulos)
    return None


def _confirmar_sensivel(argumentos: Dict[str, Any], sessao: str, turno: _Turno, alvo) -> Optional[Dict[str, Any]]:
    """Senha e dado pessoal não são travados: a pessoa confirma no cartão, a
    cada vez. A confirmação vale também como a aprovação do turno, para não
    abrir dois cartões seguidos."""
    sensivel = _sensivel(argumentos, alvo)
    if sensivel is None:
        return None
    from ..contrato import CodigoDeErro
    from ..politica import avaliar

    comando = f"computador_sensivel digitar {sensivel}"
    decisao = avaliar(comando, sessao, checar_alvo=False, na_hora=True)
    if decisao.get("permitido"):
        turno.aprovado = True
        return None
    base = {"ok": False, "executado": False, "fase": "permissao", "chave": "computador_sensivel",
            "motivo": f"digitar {sensivel}: é isso mesmo que você quer?", "comando": comando}
    if decisao.get("precisa_perguntar"):
        return {**base, "precisa_permissao": True, "so_desta_vez": True,
                "codigo_erro": CodigoDeErro.PRECISA_PERMISSAO}
    return {**base, "recusado": True, "motivo": str(decisao.get("motivo") or ""),
            "codigo_erro": CodigoDeErro.RECUSADO_POLITICA}


def _modo_depois(argumentos: Dict[str, Any]) -> str:
    """Como conferir o gesto: `texto` (padrão), `print` ou `nada`."""
    modo = str(argumentos.get("depois") or "").strip().lower()
    if modo in ("texto", "print", "nada"):
        return modo
    if argumentos.get("ver_depois") is False:
        return "nada"
    return "print" if argumentos.get("ver_depois") is True else "texto"


async def executar(argumentos: Dict[str, Any], state: Any) -> Dict[str, Any]:
    """Executa a ação. O primeiro resultado do turno informa o navegador escolhido."""
    resultado = await _executar(argumentos, state)
    with _trava:
        turno = _turnos.get(str(getattr(state, "session_id", "") or ""))
    if isinstance(resultado, dict) and turno is not None and not turno.extras.get("navegador_dito"):
        from .perfil_do_agente import navegador_escolhido

        turno.extras["navegador_dito"] = True
        resultado["navegador"] = navegador_escolhido()
    return resultado


async def _executar(argumentos: Dict[str, Any], state: Any) -> Dict[str, Any]:
    argumentos = dict(argumentos or {})
    acao = str(argumentos.get("acao") or "").strip().lower()
    if acao not in ACOES:
        dica = _SUGESTOES.get(acao)
        return {"erro": f"ação desconhecida: {acao!r}" + (f" — quis dizer {dica!r}?" if dica else ""),
                "acoes": sorted(ACOES)}
    if "computador" not in (getattr(state, "funcoes_ativas", None) or []):
        # A política já barra; esta é a segunda porta, para quem chamar direto.
        return {"erro": "o computer use exige a função @computador ligada"}
    sessao = str(getattr(state, "session_id", "") or "")

    if acao == "encerrar":
        fechou = encerrar_sessao(sessao)
        _emitir("sessao", status="encerrada")
        return {"ok": True, "executado": True, "acao": "encerrar", "encerrada": fechou,
                "motivo": "sessão do computador encerrada" if fechou else "não havia sessão aberta"}
    if acao == "pedir_a_pessoa":
        return _pedir_a_pessoa(argumentos, sessao)
    # Com a vez da pessoa, nem esperar: o agente não fica segurando o turno.
    if (recusa := _recusa_da_vez()) is not None:
        return recusa

    if acao == "esperar":
        _dizer_na_moldura(sessao, argumentos)
        segundos = max(0.0, min(float(argumentos.get("segundos") or 1.0), 30.0))
        if argumentos.get("ate_mudar"):
            return await _esperar_mudar(argumentos, max(segundos, 1.0))
        await asyncio.sleep(segundos)
        return {"ok": True, "acao": "esperar", "segundos": segundos}

    if (motivo := _contrato(acao, argumentos)) is not None:
        return {"erro": motivo}
    if (motivo := trava(acao, argumentos)) is not None:
        return {"ok": False, "executado": False, "recusado": True, "sem_apelacao": True,
                "erro": f"recusado sem apelação: {motivo}"}

    try:
        backend = _obter_backend()
    except ErroDoComputador as exc:
        return {"erro": f"computer use indisponível: {exc}"}
    try:
        tamanho = await asyncio.to_thread(backend.tamanho)
    except Exception as exc:
        return {"erro": f"computer use indisponivel: {exc}"}
    with _trava:
        retomada = _sessao_dona == sessao and sessao not in _turnos
    if (motivo := _reivindicar_sessao(sessao)) is not None:
        return {"ok": False, "executado": False, "erro": motivo}
    turno = _turno_de(sessao, state)
    epoca = computador_vez.atual().epoca
    _emitir("sessao", status="observando", retomada=retomada)
    if "cabecalho" not in turno.extras:
        turno.extras["cabecalho"] = await asyncio.to_thread(_cabecalho, sessao, backend)
    if acao in LEITURA:
        # Olhar também é usar o computador: moldura e passo no cartão, sem
        # pedir aprovação, porque ler não mexe em nada.
        if (motivo := _abrir_moldura(sessao, turno, backend)) is not None:
            logger.info("Leitura sem moldura: %s", motivo)
        _anunciar(turno, acao, argumentos)
        etiqueta = etiqueta_do_gesto(acao, argumentos)
        await _balao_da_leitura(backend, turno, etiqueta)
        try:
            resultado = await _ler(backend, turno, state, acao, argumentos, tamanho, epoca)
        except ErroDoComputador as exc:
            _emitir_gesto(turno, acao, argumentos, "erro")
            return {"ok": False, "executado": False, "erro": str(exc)}
        if resultado.get("ok"):
            _emitir_gesto(turno, acao, argumentos, "ok")
            await _balao_da_leitura(backend, turno, _o_que_leu(etiqueta, resultado))
        return resultado

    if acao in _COM_ALVO | PELA_ACESSIBILIDADE and (erro := _mirar(argumentos, turno, tamanho)) is not None:
        return {"erro": erro}
    if acao in ("digitar", "tecla", "definir_valor"):
        alvo = argumentos.get("_alvo") if acao == "definir_valor" else await asyncio.to_thread(backend.foco)
        if acao == "digitar":
            argumentos["_alvo"] = alvo  # o veredito confere o campo que tinha o foco
        if (recusa := _recusa_de_senha(acao, argumentos, alvo)) is not None:
            return recusa
        texto = argumentos.get("texto") if acao == "digitar" else argumentos.get("valor")
        if acao != "tecla" and (pedido := _confirmar_sensivel({"texto": texto}, sessao, turno, alvo)) is not None:
            return pedido

    if (recusa := await _alvo_mudou(backend, turno, acao, argumentos, tamanho)) is not None:
        return recusa
    pedido = _autorizar(acao, argumentos, sessao, turno)
    if pedido is not None:
        return pedido
    motivo = _abrir_moldura(sessao, turno, backend)
    if motivo is not None:
        return {"ok": False, "executado": False, "erro": f"computer use não começou: {motivo}"}
    return await _gesticular(backend, turno, state, acao, argumentos, tamanho, epoca)


async def _balao_da_leitura(backend: BackendDoComputador, turno: _Turno, etiqueta: str) -> None:
    """Olhar também aparece na tela: o balão ao lado do mouse, sem onda."""
    moldura = turno.moldura
    if moldura is None or not moldura.aberta:
        return
    try:
        ponto = await asyncio.to_thread(backend.posicao_do_mouse)
        await asyncio.to_thread(moldura.gesto, etiqueta, False, ponto)
    except Exception:
        logger.debug("Balão da leitura não saiu.", exc_info=True)


def _o_que_leu(etiqueta: str, resultado: Dict[str, Any]) -> str:
    if resultado.get("acao") == "elementos":
        return f"{etiqueta} · {len(resultado.get('elementos') or [])} elementos"
    if resultado.get("acao") == "janelas":
        return f"{etiqueta} · {len(resultado.get('janelas') or [])} janelas"
    if resultado.get("acao") == "programas":
        return f"{etiqueta} · {len(resultado.get('abertos') or [])} abertos"
    if resultado.get("tela_igual"):
        return f"{etiqueta} · nada mudou"
    return etiqueta


_MAX_PARA_PROCURAR = 80000
_MAX_TRECHOS = 60


def _trechos_com(texto: str, termo: str, contexto: int = 1) -> List[str]:
    """Linhas com `termo`, sem distinguir maiúsculas, com `contexto` linhas antes e depois."""
    linhas = [linha for linha in texto.split("\n") if linha.strip()]
    alvo = termo.casefold()
    trechos: List[str] = []
    vistos: set = set()
    for i, linha in enumerate(linhas):
        if alvo in linha.casefold():
            trecho = " | ".join(linhas[max(0, i - contexto): i + contexto + 1])
            # Texto e link com as mesmas palavras aparecem duas vezes na árvore.
            if trecho in vistos:
                continue
            vistos.add(trecho)
            trechos.append(trecho)
            if len(trechos) >= _MAX_TRECHOS:
                break
    return trechos


def _caixa_da_regiao(regiao: Any, tamanho: Tuple[int, int]) -> Optional[Tuple[int, int, int, int]]:
    """Converte `regiao`, em coordenadas do print, numa caixa em pixels da tela."""
    if not isinstance(regiao, dict):
        return None
    try:
        x, y = float(regiao["x"]), float(regiao["y"])
        largura, altura = float(regiao["largura"]), float(regiao["altura"])
    except (KeyError, TypeError, ValueError):
        return None
    if largura <= 0 or altura <= 0:
        return None
    x0, y0 = _para_tela(x, y, tamanho)
    x1, y1 = _para_tela(x + largura, y + altura, tamanho)
    return (x0, y0, max(x1, x0 + 1), max(y1, y0 + 1))


# Gestos cujo efeito pode aparecer só na imagem: rolagem e tecla de navegação.
_CONFERE_A_IMAGEM = frozenset({"rolar", "tecla"})


async def _miniatura_da_tela(backend: BackendDoComputador, tamanho: Tuple[int, int],
                             acao: str) -> Optional[bytes]:
    """A tela inteira em miniatura, para comparar antes e depois do gesto; None se não der."""
    if acao not in _CONFERE_A_IMAGEM:
        return None
    try:
        return await asyncio.to_thread(_miniatura, backend, (0, 0, tamanho[0], tamanho[1]))
    except Exception:
        return None


def _miniatura(backend: BackendDoComputador, caixa: Tuple[int, int, int, int]) -> bytes:
    import io

    from PIL import Image

    imagem = Image.open(io.BytesIO(backend.recorte(caixa).png))
    return imagem.convert("L").resize((96, 54)).tobytes()


def _diferentes(antes: bytes, agora: bytes, ignorar: frozenset = frozenset()) -> bool:
    """True quando mais de 1% dos pontos considerados mudou acima do limiar.

    `ignorar` são os pontos que se animam sozinhos e ficam fora da conta.
    """
    considerados = len(antes) - len(ignorar)
    if considerados <= 0:
        return False
    mudados = sum(1 for i, (a, b) in enumerate(zip(antes, agora)) if i not in ignorar and abs(a - b) > 24)
    return mudados > considerados // 100


def _animados(amostras: List[bytes]) -> frozenset:
    """Pontos que variam entre amostras tiradas com a tela parada: animação da própria tela."""
    return frozenset(i for i in range(len(amostras[0]))
                     if max(a[i] for a in amostras) - min(a[i] for a in amostras) > 24)


async def _esperar_mudar(argumentos: Dict[str, Any], segundos: float) -> Dict[str, Any]:
    """Espera a tela, ou a `regiao`, mudar, até `segundos`."""
    import time

    try:
        backend = _obter_backend()
        tamanho = await asyncio.to_thread(backend.tamanho)
    except ErroDoComputador as exc:
        return {"erro": f"computer use indisponível: {exc}"}
    caixa = _caixa_da_regiao(argumentos.get("regiao"), tamanho) or (0, 0, tamanho[0], tamanho[1])
    inicio = time.monotonic()
    # Três amostras seguidas marcam o que se anima sozinho (personagem parado,
    # nuvem, relógio); só o resto conta como mudança.
    amostras = [await asyncio.to_thread(_miniatura, backend, caixa)]
    for _ in range(2):
        await asyncio.sleep(0.15)
        amostras.append(await asyncio.to_thread(_miniatura, backend, caixa))
    ignorar = _animados(amostras)
    antes = amostras[-1]
    extra = ({"animacao_ignorada": f"{round(100 * len(ignorar) / len(antes))}% da área"} if ignorar else {})
    while time.monotonic() - inicio < segundos:
        await asyncio.sleep(0.4)
        agora = await asyncio.to_thread(_miniatura, backend, caixa)
        if _diferentes(antes, agora, ignorar):
            return {"ok": True, "acao": "esperar", "mudou": True,
                    "depois_de_s": round(time.monotonic() - inicio, 1), **extra}
    return {"ok": True, "acao": "esperar", "mudou": False, "segundos": segundos, **extra,
            "nota": "a tela não mudou nesse tempo: confira com `ver` se o gesto anterior pegou"}


async def _ler(backend: BackendDoComputador, turno: _Turno, state: Any, acao: str,
               argumentos: Dict[str, Any], tamanho: Tuple[int, int], epoca: int) -> Dict[str, Any]:
    """Ações de leitura: `ver`, `elementos`, `janelas`, `programas` e `ler`."""
    if acao == "ler":
        from .computador_uia import MAX_TEXTO

        procurar = str(argumentos.get("procurar") or "").strip()
        texto = await _ler_texto(backend, _MAX_PARA_PROCURAR if procurar else MAX_TEXTO, procurar)
        if computador_vez.atual().epoca != epoca:
            return _anulado(turno, acao, argumentos)
        if procurar:
            try:
                contexto = max(0, min(int(argumentos.get("contexto") or 1), 6))
            except (TypeError, ValueError):
                contexto = 1
            trechos = _trechos_com(texto, procurar, contexto)
            return {"ok": True, "acao": "ler", "procurar": procurar, "trechos": trechos,
                    "nota": (f"{len(trechos)} trechos com «{procurar}», com {contexto} linha(s) de antes e de "
                             "depois; se faltar o nome do item, peça de novo com `contexto` maior"
                             if trechos else _sem_trechos(procurar))}
        return {"ok": True, "acao": "ler", "texto": texto,
                **({"cortado": True, "dica": "use `procurar` para ir direto ao que interessa"}
                   if len(texto) >= MAX_TEXTO else {}),
                "nota": ("o texto da janela da frente, sem print. O que é desenho (jogo, imagem, canvas, "
                         "modelo 3D) não aparece aqui: para isso, `ver`")}
    if acao == "ver":
        await _estabilizar(backend, turno, tamanho)
    if acao == "ver" and argumentos.get("regiao") is not None:
        caixa = _caixa_da_regiao(argumentos.get("regiao"), tamanho)
        if caixa is None:
            return {"erro": "`regiao` precisa de x, y, largura e altura, no espaço do print"}
        tela = await asyncio.to_thread(backend.recorte, caixa)
        if computador_vez.atual().epoca != epoca:
            return _anulado(turno, acao, argumentos)
        entrega = await _entregar_print(state, tela, turno, {})
        regiao = argumentos["regiao"]
        ampliado = round(tela.largura / max(1.0, float(regiao["largura"])), 2)
        return {"ok": True, "acao": "ver", "recorte": {**{k: regiao[k] for k in ("x", "y", "largura", "altura")},
                                                        "ampliado": ampliado},
                "nota": ("recorte ampliado só para ler. Para clicar, use a coordenada do print inteiro: "
                         "x = recorte.x + x_no_recorte / ampliado, e o mesmo para y"), **entrega}
    if acao == "programas":
        dados = await asyncio.to_thread(backend.programas, str(argumentos.get("nome") or "").strip())
        if computador_vez.atual().epoca != epoca:
            return _anulado(turno, acao, argumentos)
        return {"ok": True, "acao": "programas", **dados,
                "nota": ("aberto: `focar` pelo título; instalado e fechado: `abrir` pelo `nome`; "
                         "rodando sem janela: está na bandeja, `abrir` costuma trazer a janela")}
    if acao == "janelas":
        janelas = await asyncio.to_thread(backend.janelas)
        if computador_vez.atual().epoca != epoca:
            return _anulado(turno, acao, argumentos)
        for janela in janelas:
            janela["x"], janela["y"] = _para_print(int(janela["x"]), int(janela["y"]), tamanho)
            janela["largura"], janela["altura"] = _para_print(int(janela["largura"]),
                                                              int(janela["altura"]), tamanho)
        return {"ok": True, "acao": "janelas", "janelas": janelas,
                "nota": "coordenadas no espaço do print"}
    if acao == "elementos":
        achados, aviso = await _ler_elementos(backend, tamanho)
        if computador_vez.atual().epoca != epoca:
            return _anulado(turno, acao, argumentos)
        _numerar(turno, achados)
        if aviso:
            return {"ok": True, "acao": "elementos", "elementos": [], "elementos_aviso": aviso}
        turno.extras["leituras"] = int(turno.extras.get("leituras") or 0) + 1
        return {"ok": True, "acao": "elementos", "elementos": para_o_modelo(achados, escala(tamanho)),
                "nota": ("sem print: mire com `elemento: n`. A lista é da janela em foco, do menu e da barra; "
                         "o que está atrás deles só aparece no `ver`")}
    tela, marcas = await _tirar_print(backend, turno, tamanho, bool(argumentos.get("marcar")))
    if computador_vez.atual().epoca != epoca:
        return _anulado(turno, acao, argumentos)
    if _mesmo_print(turno, tela):
        marcas["tela_igual"] = "idêntica ao print anterior: nada mudou na tela desde ele"
    entrega = await _entregar_print(state, tela, turno, marcas)
    mouse = _para_print(*(await asyncio.to_thread(backend.posicao_do_mouse)), tamanho)
    nota = ("o print vai anexado como imagem à sua próxima chamada; clique pela coordenada dele"
            if "elementos" not in marcas else
            "o print vai anexado com um número sobre cada elemento; mire com `elemento: n`, "
            "e use `x`/`y` só para o que não tem número")
    return {"ok": True, "acao": "ver", "print": {"largura": tela.largura, "altura": tela.altura},
            "mouse": {"x": mouse[0], "y": mouse[1]}, **marcas, "nota": nota, **entrega}


async def _gesticular(backend: BackendDoComputador, turno: _Turno, state: Any, acao: str,
                      argumentos: Dict[str, Any], tamanho: Tuple[int, int], epoca: int) -> Dict[str, Any]:
    descricao = descrever(acao, argumentos)
    _anunciar(turno, acao, argumentos)
    # A moldura do turno, guardada aqui: o Assumir pode tirá-la do turno no meio do gesto.
    moldura = turno.moldura
    if (recusa := await _janela_trocou(backend, turno, acao, argumentos)) is not None:
        return recusa
    imagem_antes = await _miniatura_da_tela(backend, tamanho, acao)
    try:
        moldura.conferir()
        # A thread do gesto não para sozinha; esta checagem solta a tecla segurada
        # no atalho, no encerramento ou no prazo.
        interromper = lambda: (moldura.parada or turno.interrompido.is_set()  # noqa: E731
                               or computador_vez.atual().epoca != epoca)
        try:
            await asyncio.to_thread(_executar_gesto, backend, acao, argumentos, tamanho, interromper)
        except asyncio.CancelledError:
            turno.interrompido.set()
            raise
        finally:
            await _guardar_frente(backend, turno, acao, argumentos)
        turno.gestos += 1
        turno.extras["gestos_desde_o_print"] = int(turno.extras.get("gestos_desde_o_print") or 0) + 1
        turno.extras["ultimo_gesto_em"] = time.monotonic()
        # O balão diz o gesto e o clique ganha a onda, depois do gesto: o mouse
        # já está lá. Pela acessibilidade o mouse fica parado, e os dois vão
        # sobre o elemento.
        alvo = argumentos.get("_alvo") if acao in PELA_ACESSIBILIDADE else None
        ponto = getattr(alvo, "centro", None) or await asyncio.to_thread(backend.posicao_do_mouse)
        await asyncio.to_thread(moldura.gesto, etiqueta_do_gesto(acao, argumentos),
                                acao.startswith("clicar") or acao == "invocar", ponto)
    except MolduraParada:
        if computador_vez.atual().epoca != epoca:
            return _anulado(turno, acao, argumentos)
        _emitir_gesto(turno, acao, argumentos, "parado")
        return {"ok": False, "executado": False, "erro": "parado pelo atalho: nenhum gesto roda depois dele"}
    except ErroDoComputador as exc:
        _emitir_gesto(turno, acao, argumentos, "erro")
        return {"ok": False, "executado": False, "erro": str(exc)}
    if computador_vez.atual().epoca != epoca:
        return _anulado(turno, acao, argumentos)

    resultado: Dict[str, Any] = {"ok": True, "executado": True, "acao": acao,
                                 "feito": descricao + (f" ({argumentos['_feito']})" if argumentos.get("_feito") else ""),
                                 **_aviso_do_digitado(acao, argumentos)}
    # Onde o gesto caiu: o elemento diz de que janela veio; sem elemento, a janela
    # em foco. Um campo de chat de outro app parece o certo até ler o nome dele.
    janela = str(getattr(argumentos.get("_alvo"), "janela", "") or "")
    if not janela:
        try:
            janela = str(next((j.get("titulo") for j in await asyncio.to_thread(backend.janelas)
                               if j.get("em_foco")), "") or "")
        except Exception:
            janela = ""
    if janela:
        resultado["na_janela"] = janela
        resultado.update(_foco_mudou(turno, acao, argumentos, janela))
    modo = _modo_depois(argumentos)
    if modo != "nada":
        await asyncio.sleep(_ESPERA_DEPOIS_DO_GESTO)
    if modo == "texto":
        # Menos print: quando a janela se descreve, a lista de elementos diz
        # se o gesto fez efeito, e custa texto em vez de imagem.
        conferencia = await _conferir_por_texto(backend, turno, tamanho, acao, argumentos)
        if computador_vez.atual().epoca != epoca:
            return _anulado(turno, acao, argumentos)
        if conferencia is None:
            modo = "print"
        else:
            resultado.update(conferencia)
            if getattr(state, "print_da_tela", None):
                # O print de antes do gesto mostra uma tela que não existe mais, e iria anexado
                # a toda chamada seguinte: sai, e o `ver` traz um novo se precisar.
                state.print_da_tela = None
                resultado["print_anterior"] = "saiu: a tela mudou; peça `ver` se precisar olhar de novo"
    if modo == "print":
        try:
            tela, marcas = await _tirar_print(backend, turno, tamanho, bool(argumentos.get("marcar")))
            if computador_vez.atual().epoca != epoca:
                return _anulado(turno, acao, argumentos)
            igual = _mesmo_print(turno, tela)
            resultado["print"] = {"largura": tela.largura, "altura": tela.altura}
            resultado.update(marcas)
            resultado.update(await _entregar_print(state, tela, turno, marcas))
            if igual:
                resultado.update(veredito="sem_efeito_aparente", tela_igual=True,
                                 proximo=("o print saiu idêntico ao de antes do gesto: nada mudou na tela. "
                                          "Não repita o mesmo gesto; procure o motivo"))
        except ErroDoComputador as exc:
            resultado["print_falhou"] = str(exc)
        if "veredito" not in resultado:
            resultado.update(veredito(acao, argumentos, None, None, None))
    if _janela_pedida_na_frente(acao, argumentos, janela):
        resultado.update(veredito="confirmado", proximo=f"«{janela}» está na frente; não repita")
    if resultado.get("veredito") == "sem_efeito_aparente" and imagem_antes is not None:
        imagem_agora = await _miniatura_da_tela(backend, tamanho, acao)
        if imagem_agora is not None and _diferentes(imagem_antes, imagem_agora):
            resultado.update(veredito="mudou", mudou_na_imagem=True,
                             proximo=("a lista ficou igual, mas a imagem da tela mudou (rolagem, desenho ou "
                                      "algo que a lista não descreve): peça `ver` se precisar conferir; não repita"))
    if resultado.get("veredito") == "sem_efeito_aparente":
        # Os gestos que vinham na mesma sequência contavam com este.
        resultado["parar_sequencia"] = "o gesto não fez efeito aparente; os passos seguintes não rodaram"
    _emitir_gesto(turno, acao, argumentos, "ok")
    return resultado


def _janela_pedida_na_frente(acao: str, argumentos: Dict[str, Any], janela: str) -> bool:
    """`focar` e `abrir` se provam pela janela em foco, não pela lista: trazer
    para a frente uma janela que já estava lá não muda nada e ainda assim deu certo."""
    if acao not in ("focar", "abrir") or not janela:
        return False
    # O `abrir` já conferiu: achou a janela do programa e a trouxe (o nome
    # pedido pode ser outro, como "notepad" para o Bloco de notas).
    if acao == "abrir" and "na frente" in str(argumentos.get("_feito") or ""):
        return True
    import unicodedata

    def normal(texto: Any) -> str:
        sem_acento = unicodedata.normalize("NFKD", str(texto or ""))
        return "".join(c for c in sem_acento if not unicodedata.combining(c)).casefold().strip()

    pedido = normal(argumentos.get("janela") if acao == "focar" else argumentos.get("nome"))
    return bool(pedido) and pedido in normal(janela)


def _so_atalho_do_sistema(argumentos: Dict[str, Any]) -> bool:
    """True quando toda combinação usa Win, que vai para o sistema e não para a janela."""
    try:
        combinacoes = [ler_combinacao(c) for c in _combinacoes(argumentos)]
    except ErroDoComputador:
        return False
    return bool(combinacoes) and all("win" in mods or tecla == "win" for mods, tecla in combinacoes)


def _foco_mudou(turno: _Turno, acao: str, argumentos: Dict[str, Any], janela: str) -> Dict[str, Any]:
    """Tecla vai para a janela da frente. Se ela trocou desde o último gesto, o texto pode
    ter caído num aviso ou em outro programa, e o `ok` sozinho não mostra isso."""
    anterior = turno.extras.get("janela_do_ultimo_gesto")
    turno.extras["janela_do_ultimo_gesto"] = janela
    if acao not in ("digitar", "tecla") or argumentos.get("_alvo") is not None:
        return {}
    if not anterior or anterior == janela:
        return {}
    return {"foco_mudou": {"de": anterior, "para": janela,
                           "nota": "confira se o teclado devia ir para esta janela antes de seguir"}}


async def _guardar_frente(backend: BackendDoComputador, turno: _Turno, acao: str,
                         argumentos: Dict[str, Any]) -> None:
    """Guarda o programa da frente e os que tinham janela, para a trava do próximo gesto."""
    if acao == "tecla" and _so_atalho_do_sistema(argumentos):
        # O que o atalho abre aparece depois; o próximo gesto define a frente.
        turno.extras.pop("frente", None)
        turno.extras.pop("abertas", None)
        return
    try:
        turno.extras["frente"] = await asyncio.to_thread(backend.programa_da_frente)
        turno.extras["abertas"] = await asyncio.to_thread(backend.janelas_visiveis)
    except Exception:
        turno.extras.pop("frente", None)
        turno.extras.pop("abertas", None)


async def _janela_trocou(backend: BackendDoComputador, turno: _Turno, acao: str,
                         argumentos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Recusa `digitar` e `tecla` quando o programa da frente mudou desde o último gesto."""
    antes = turno.extras.get("frente")
    if acao not in ("digitar", "tecla") or antes is None:
        return None
    if acao == "tecla" and _so_atalho_do_sistema(argumentos):
        return None
    try:
        agora = await asyncio.to_thread(backend.programa_da_frente)
    except Exception:
        return None
    if agora is None or agora == antes:
        return None
    abertas = turno.extras.get("abertas")
    if abertas is not None:
        try:
            janela = await asyncio.to_thread(backend.janela_da_frente)
        except Exception:
            janela = None
        if janela is not None and janela not in abertas:
            # A janela não existia no gesto anterior: foi o gesto que a abriu.
            turno.extras["frente"] = agora
            return None
    try:
        titulo = next((j.get("titulo") for j in await asyncio.to_thread(backend.janelas) if j.get("em_foco")), "")
    except Exception:
        titulo = ""
    return {"ok": False, "executado": False, "na_janela": str(titulo or ""),
            "erro": (f"outro programa veio para a frente desde o último gesto (agora: «{titulo or 'outra janela'}»): "
                     "nada foi digitado. Traga a janela certa com `focar` e repita")}


def _limpar_para_testes() -> None:
    global _dono, _sessao_dona
    with _trava:
        for sessao, turno in list(_turnos.items()):
            _encerrar(sessao, turno)
        _turnos.clear()
        _dono = None
        _sessao_dona = None
    _ACORDADAS.clear()
    computador_vez._limpar_para_testes()


__all__ = ["ACOES", "GESTOS", "LEITURA", "assumir", "parar_turno", "descrever", "devolver", "encerrar_sessao",
           "encerrar_todos", "sessao_ativa", "encerrar_turno", "escala", "executar", "ouvir_fala", "trava",
           "usar_backend"]
