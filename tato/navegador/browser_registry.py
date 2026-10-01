"""Seleciona e despacha o provedor de navegador configurado.

O Chrome local usa somente abas criadas pela extensão.
A perda da extensão não troca de provedor sem aviso.
"""
from __future__ import annotations

import inspect
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from .browser_contracts import Action, BrowserCapability, Observation
from .browser_provider import BrowserProvider

logger = logging.getLogger(__name__)

_PROVEDORES: Dict[str, BrowserProvider] = {}
_SESSOES: Dict[Tuple[str, str], BrowserCapability] = {}
# Quando cada sessao foi usada pela ultima vez, e quantas acoes estao rodando
# nela agora. Sem isto o navegador remoto de uma conversa ficava aberto ate o
# processo morrer: memoizado por conversa, nunca fechado.
_ULTIMO_USO: Dict[Tuple[str, str], float] = {}
_EM_USO: Dict[Tuple[str, str], int] = {}
_LOCK = threading.RLock()


def registrar(provider: BrowserProvider) -> None:
    if not isinstance(provider, BrowserProvider):
        raise TypeError(f"provedor precisa herdar BrowserProvider, veio {type(provider).__name__}")
    nome = str(provider.name or "").strip()
    if not nome:
        raise ValueError("provedor de navegador precisa de name")
    with _LOCK:
        _PROVEDORES[nome] = provider


def listar() -> List[BrowserProvider]:
    with _LOCK:
        return sorted(_PROVEDORES.values(), key=lambda item: item.name)


def obter(nome: str) -> Optional[BrowserProvider]:
    with _LOCK:
        return _PROVEDORES.get(str(nome or "").strip())


def _disponivel(provider: BrowserProvider) -> bool:
    try:
        return bool(provider.is_available())
    except Exception:
        logger.warning("Provedor de navegador %s falhou ao verificar disponibilidade", provider.name, exc_info=True)
        return False


def resolver(configurado: str | None = None) -> Optional[BrowserProvider]:
    """Chrome é padrão mesmo desconectado: não migra a sessão silenciosamente."""
    if configurado:
        return obter(configurado)
    chrome = obter("chrome_tabs")
    if chrome is not None:
        return chrome
    return next((provider for provider in listar() if _disponivel(provider)), None)


def _capability(provider: BrowserProvider, task_id: str) -> tuple[BrowserCapability, bool]:
    """Reusa o navegador da tarefa; só recria quando a conexão realmente morreu."""
    chave = (task_id, provider.name)
    with _LOCK:
        _ULTIMO_USO[chave] = time.time()
        existente = _SESSOES.get(chave)
        if existente is not None:
            esta_viva = getattr(existente, "esta_viva", None)
            try:
                viva = esta_viva is None or bool(esta_viva())
            except Exception:
                logger.warning("Falha ao verificar sessao do navegador %s", existente.session_id, exc_info=True)
                viva = False
            if viva:
                return existente, True
            _SESSOES.pop(chave, None)
            _ULTIMO_USO.pop(chave, None)
            try:
                provider.close_session(existente.session_id)
            except Exception:
                logger.debug("Falha ao recolher sessão morta %s", existente.session_id, exc_info=True)
        criada = provider.create_session(task_id)
        if not isinstance(criada, BrowserCapability):
            raise TypeError("provedor criou sessão fora do contrato")
        _SESSOES[chave] = criada
        return criada, False


def _minutos_ocioso() -> float:
    try:
        from ..config import settings

        return float(getattr(settings, "TATO_BROWSER_OCIOSO_MIN", 0))
    except Exception:
        return 0.0


def _fechar(chaves) -> int:
    with _LOCK:
        sessoes = [(chave, _SESSOES.pop(chave)) for chave in chaves if chave in _SESSOES]
        for chave, _ in sessoes:
            _ULTIMO_USO.pop(chave, None)
    fechadas = 0
    for (_, nome), capability in sessoes:
        provider = obter(nome)
        if provider is None:
            continue
        try:
            if provider.close_session(capability.session_id):
                fechadas += 1
        except Exception:
            logger.warning("Falha ao fechar sessão %s de %s", capability.session_id, nome, exc_info=True)
    return fechadas


def fechar_ociosos(minutos: Optional[float] = None, *, agora: Optional[float] = None) -> int:
    """Fecha o navegador que ninguem usa ha `minutos`. Nunca o que esta rodando.

    Uma acao ou um roteiro em curso contam como uso: um roteiro longo nao pode
    ter a pagina fechada no meio porque comecou ha mais de dez minutos.
    """
    limite = _minutos_ocioso() if minutos is None else float(minutos)
    if limite <= 0:
        return 0
    corte = (time.time() if agora is None else agora) - limite * 60
    with _LOCK:
        velhas = [chave for chave, quando in _ULTIMO_USO.items()
                  if chave[1] != "chrome_tabs" and quando < corte and not _EM_USO.get(chave)]
    if velhas:
        logger.info("Fechando %d navegador(es) ocioso(s) ha mais de %.0f min.", len(velhas), limite)
    return _fechar(velhas)


def encerrar_todos() -> int:
    """Na saida, fecha as sessoes remotas; abas do usuario ficam no Chrome."""
    with _LOCK:
        chaves = [chave for chave in _SESSOES if chave[1] != "chrome_tabs"]
    return _fechar(chaves)


def encerrar(task_id: str, provider_name: str | None = None) -> int:
    """Fecha explicitamente as sessões de uma tarefa e devolve quantas saíram."""
    dono = str(task_id or "browser")
    with _LOCK:
        chaves = [
            chave for chave in _SESSOES
            if chave[0] == dono and (provider_name is None or chave[1] == provider_name)
        ]
    return _fechar(chaves)


def esta_aberta(task_id: str) -> bool:
    """Consulta a sessão existente sem iniciar navegador nem alterar a página."""
    with _LOCK:
        sessoes = [sessao for (dono, _), sessao in _SESSOES.items() if dono == task_id]
    for sessao in sessoes:
        viva = getattr(sessao, "esta_viva", None)
        try:
            if viva is None or bool(viva()):
                return True
        except Exception:
            logger.debug("Falha ao verificar sessão %s", sessao.session_id, exc_info=True)
    return False


async def despachar(
    action: Action,
    state,
    user_message: str = "",
    *,
    provider_name: str | None = None,
) -> dict:
    """Aplica a policy antes de consultar ou criar qualquer navegador."""
    from ..politica import portao

    decisao = portao.evaluate(state, "browser", user_message, action.para_dict())
    trace = {
        "proposed_tool": "browser",
        "browser_action": action.tipo,
        "harness_allowed": decisao.allowed,
        "harness_reason": decisao.reason,
    }
    if not decisao.allowed:
        return {
            "ok": False,
            "executado": False,
            "fase": "policy",
            "motivo": decisao.reason,
            "faltando": decisao.missing,
            "trace": trace,
        }

    task_id = str(getattr(state, "session_id", "") or "browser")
    if action.tipo == "encerrar":
        fechadas = encerrar(task_id, provider_name)
        return {
            "ok": True,
            "executado": True,
            "encerradas": fechadas,
            "motivo": "sessão do navegador encerrada" if fechadas else "não havia sessão aberta",
            "trace": trace,
        }

    if action.pede_autorizacao:
        pedido = _autorizacao(action, state)
        if pedido is not None:
            pedido["trace"] = trace
            return pedido

    provider = resolver(provider_name)
    if provider is None:
        motivo = (
            f"provedor de navegador '{provider_name}' não está registrado"
            if provider_name else "nenhum provedor de navegador disponível"
        )
        return {"ok": False, "executado": False, "fase": "provider", "motivo": motivo, "trace": trace}
    if not _disponivel(provider):
        motivo = ("extensão do Chrome desconectada; conecte-a para continuar na mesma aba "
                  "(o Tato não muda para outro navegador sozinho)"
                  if provider.name == "chrome_tabs" else
                  f"provedor de navegador '{provider.name}' está indisponível")
        return {
            "ok": False,
            "executado": False,
            "fase": "provider",
            "motivo": motivo,
            "trace": trace,
        }

    chave_em_uso = (task_id, provider.name)
    with _LOCK:
        # Dois turnos no mesmo navegador: a regra e RECUSA. A sessao so
        # serializava cada acao, entao a acao do turno novo entrava entre os
        # passos do roteiro do turno anterior e mudava a pagina debaixo dele.
        # Fila deixaria o turno novo parado em silencio atras de um roteiro
        # longo; recusar na hora deixa o agente dizer o que esta acontecendo.
        if _EM_USO.get(chave_em_uso):
            return {
                "ok": False,
                "executado": False,
                "fase": "ocupado",
                "motivo": ("o navegador desta conversa esta ocupado com uma acao anterior "
                           "(provavelmente um roteiro). Diga isso a pessoa; tente de novo "
                           "quando ele terminar."),
                "trace": trace,
            }
        _EM_USO[chave_em_uso] = 1
    cartao = _Cartao(action)
    try:
        capability, reutilizada = _capability(provider, task_id)
        # Uma segunda ação no mesmo turno já está no cartão corrente. O selo
        # "continuação" só vale quando a conexão atravessou uma resposta.
        retomada = reutilizada and not bool(getattr(state, "navegador_do_turno", None))
        cartao.comecar(capability, retomada=retomada)
        if action.tipo == "roteiro":
            from .browser_roteiro import executar as executar_roteiro

            resultado = await executar_roteiro(capability, action, avisar=cartao.passo)
            cartao.terminar(capability, "ok" if resultado.get("ok") else "parou",
                            "" if resultado.get("ok") else str(resultado.get("motivo") or ""))
            return _print_como_imagem({**resultado, "executado": True, "provider": provider.name,
                                       "trace": trace, **_aviso_do_digitado(action)}, state)
        cartao.passo(0, "running")
        observacao = capability.execute(action)
        if inspect.isawaitable(observacao):
            observacao = await observacao
        if not isinstance(observacao, Observation):
            raise TypeError("provedor devolveu observação fora do contrato")
        cartao.passo(0, "ok", _leitura(action.tipo))
        cartao.terminar(capability, "ok")
        return _print_como_imagem({
            "ok": True,
            "executado": True,
            "provider": provider.name,
            "observation": observacao.para_dict(),
            "trace": trace,
            **_aviso_do_digitado(action),
        }, state)
    except Exception as exc:
        cartao.terminar(None, "erro", f"{type(exc).__name__}: {exc}"[:160])
        return {
            "ok": False,
            "executado": False,
            "fase": "execucao",
            "motivo": f"{type(exc).__name__}: {exc}"[:240],
            "trace": trace,
        }
    finally:
        with _LOCK:
            restantes = _EM_USO.get(chave_em_uso, 1) - 1
            if restantes > 0:
                _EM_USO[chave_em_uso] = restantes
            else:
                _EM_USO.pop(chave_em_uso, None)
            if chave_em_uso in _SESSOES:
                # O relogio do ocioso conta do FIM da acao, nao do comeco.
                _ULTIMO_USO[chave_em_uso] = time.time()


def _tamanho_do_png(dados: str) -> tuple[int, int]:
    """Largura e altura lidas do cabeçalho do PNG, sem decodificar a imagem."""
    import base64

    try:
        cabeca = base64.b64decode(dados[:32])
    except ValueError:
        return 0, 0
    if cabeca[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    return int.from_bytes(cabeca[16:20], "big"), int.from_bytes(cabeca[20:24], "big")


def _print_como_imagem(resultado: dict, state) -> dict:
    """Anexa a captura como imagem na próxima chamada do modelo."""
    observacao = resultado.get("observation")
    if not isinstance(observacao, dict) or not (observacao.get("metadados") or {}).get("screenshot"):
        return resultado
    dados = observacao.get("dados")
    if not isinstance(dados, str) or not dados:
        return resultado
    largura, altura = _tamanho_do_png(dados)
    state.print_da_tela = {"data_url": "data:image/png;base64," + dados, "largura": largura, "altura": altura}
    nota = "o print vai anexado como imagem à sua próxima chamada"
    return {**resultado, "observation": {**observacao, "dados": nota}}


def _leitura(tipo: str) -> str:
    """Classifica a observação do passo como texto ou captura."""
    if tipo in ("imagens", "ver"):
        return "print"
    if tipo == "console":
        return ""
    return "texto"


def _rotulo_do_passo(passo: Action | dict, nome_do_alvo) -> str:
    from .browser_sobreposicao import rotulo

    if isinstance(passo, Action):
        tipo, args = passo.tipo, passo.argumentos
    else:
        tipo = str(passo.get("acao") or "").strip().lower()
        args = passo.get("argumentos") or {}
        texto = str(args.get("texto") or passo.get("texto") or "").strip()
        if tipo in ("esperar", "conferir"):
            return f"{tipo} · “{texto[:48]}”" if texto else tipo
        if tipo == "ver":
            return "ver · print da página"
        if tipo == "pensar":
            return "pensar"
    if tipo == "snapshot":
        return "ler a página"
    if tipo == "imagens":
        return "print da página"
    return rotulo(tipo, args, nome_do_alvo(str(args.get("ref") or "")))


def _site(capability, action: Action | None = None) -> str:
    from urllib.parse import urlparse

    url = ""
    if action is not None and action.tipo == "navegar":
        url = str(action.argumentos.get("url") or "")
    if not url:
        pagina = getattr(capability, "_page", None)
        url = str(getattr(pagina, "url", "") or "")
    return urlparse(url).netloc


class _Cartao:
    """Emite eventos de início, progresso e fim da navegação."""

    def __init__(self, action: Action) -> None:
        self.action = action

    def comecar(self, capability, *, retomada: bool = False) -> None:
        from ..emissor import emitir

        nome = getattr(capability, "_nome_do_alvo", None) or (lambda ref: "")
        passos = (list(self.action.argumentos.get("passos") or [])
                  if self.action.tipo == "roteiro" else [self.action])
        emitir({"type": "navegador", "fase": "inicio", "site": _site(capability, self.action),
                "retomada": retomada,
                "passos": [{"acao": p.tipo if isinstance(p, Action) else str(p.get("acao") or ""),
                            "rotulo": _rotulo_do_passo(p, nome)} for p in passos]})

    def passo(self, indice: int, status: str, leitura: str = "") -> None:
        from ..emissor import emitir

        emitir({"type": "navegador", "fase": "passo", "indice": indice,
                "status": status, "leitura": leitura})

    def terminar(self, capability, status: str, motivo: str = "") -> None:
        from ..emissor import emitir

        emitir({"type": "navegador", "fase": "fim", "status": status, "motivo": motivo,
                "site": _site(capability) if capability is not None else ""})


def _aviso_do_digitado(action: Action) -> dict:
    """Sinaliza dados sensíveis digitados sem interromper a ação.

    Uma ação de envio continua sujeita à confirmação da sua consequência.
    """
    from ..segredos import ROTULOS, detectar

    passos = action.passos() if action.tipo == "roteiro" else [action]
    achados: list = []
    for passo in passos:
        if passo.tipo == "digitar":
            for tipo in detectar(str(passo.argumentos.get("texto") or "")):
                if tipo not in achados:
                    achados.append(tipo)
    if not achados:
        return {}
    lista = ", ".join(ROTULOS[t] for t in achados)
    return {
        "dado_sensivel": achados,
        "aviso": f"foi digitado na pagina o que parece ser {lista}; avise a pessoa em uma frase, sem repetir o valor",
    }


def _autorizacao(action: Action, state) -> Optional[dict]:
    """None quando pode rodar; senao a pergunta ou a recusa, como no shell."""
    from ..contrato import CodigoDeErro
    from ..politica import avaliar

    decisao = avaliar(action.chave_de_permissao, str(getattr(state, "session_id", "") or ""),
                      checar_alvo=False, na_hora=action.so_na_hora)
    if decisao.get("permitido"):
        return None
    base = {"ok": False, "executado": False, "fase": "permissao",
            "chave": action.chave_de_permissao, "motivo": str(decisao.get("motivo") or ""),
            "comando": descrever(action)}
    if decisao.get("precisa_perguntar"):
        # O cartao so deve oferecer "so desta vez": as outras respostas nao
        # liberariam a proxima candidatura, e oferece-las seria mentir.
        return {**base, "precisa_permissao": True, "so_desta_vez": action.so_na_hora,
                "codigo_erro": CodigoDeErro.PRECISA_PERMISSAO}
    return {**base, "recusado": True, "codigo_erro": CodigoDeErro.RECUSADO_POLITICA}


def descrever(action: Action) -> str:
    """O que a pessoa le no cartao: a acao e o alvo, nao so a chave."""
    if action.tipo == "roteiro":
        # A pessoa aprova o roteiro inteiro, entao le os passos que agem.
        passos = ", ".join(descrever(p).split("_", 1)[-1] for p in action.passos() if p.pede_autorizacao)
        return f"{action.chave_de_permissao} {passos}"[:240].strip()
    args = action.argumentos
    alvo = args.get("url") or args.get("ref") or args.get("seletor") or args.get("texto") or ""
    return f"{action.chave_de_permissao} {str(alvo)[:160]}".strip()


async def trazer_para_frente(task_id: str) -> bool:
    """O "Mostrar janela" do cartao: a janela do navegador desta conversa vem
    para a frente. Falso quando nao ha janela (nada aberto, ou headless)."""
    with _LOCK:
        sessoes = [sessao for (tarefa, _), sessao in _SESSOES.items() if tarefa == task_id]
    for sessao in sessoes:
        trazer = getattr(sessao, "trazer_para_frente", None)
        if trazer is None or getattr(sessao, "_headless", False):
            continue
        try:
            if await trazer():
                return True
        except Exception:
            logger.debug("Nao consegui trazer a janela de %s para a frente.", task_id, exc_info=True)
    return False


def _limpar_para_testes() -> None:
    with _LOCK:
        sessoes = list(_SESSOES.items())
        _SESSOES.clear()
        _ULTIMO_USO.clear()
        _EM_USO.clear()
        provedores = dict(_PROVEDORES)
        _PROVEDORES.clear()

    for (_, nome), capability in sessoes:
        provider = provedores.get(nome)
        if provider is None:
            continue
        try:
            provider.close_session(capability.session_id)
        except Exception:
            logger.debug("Falha ao limpar sessão de teste", exc_info=True)


__all__ = ["despachar", "encerrar", "encerrar_todos", "esta_aberta", "fechar_ociosos", "listar", "obter",
           "registrar", "resolver"]
