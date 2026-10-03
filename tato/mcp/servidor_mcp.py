"""Expõe `computador` e `browser` por MCP sobre JSON-RPC 2.0.

Aplica as políticas das ferramentas antes de executar cada ação.
Agrupa chamadas em turnos e renova a aprovação após o período ocioso.
Solicita aprovação por `elicitation/create` quando o cliente oferece suporte.
Publica progresso e atende cancelamentos durante a execução.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

FERRAMENTAS = ("computador", "browser", "gravacao")
OCIOSO_S = 90.0
_PRAZO_DA_PERGUNTA_S = 300.0
_VERSAO = "2025-06-18"


def esquemas() -> list:
    """As ferramentas com descrição e orientação, no formato do MCP."""
    ferramentas = json.loads((Path(__file__).parent.parent / "ferramentas.json").read_text(encoding="utf-8"))
    return [{"name": f["name"], "description": f.get("description", ""),
             "inputSchema": f.get("parameters") or {"type": "object", "properties": {}},
             **({"annotations": f["annotations"]} if f.get("annotations") else {})}
            for f in ferramentas if f.get("name") in FERRAMENTAS]


def aquecer() -> None:
    """Carrega o que a primeira chamada usaria. Sem isto ela pagava segundos de
    import antes de qualquer sinal na tela, e parecia travada."""
    from .. import politica  # noqa: F401
    from ..computador.computador import _obter_backend
    from ..navegador import browser_registry  # noqa: F401

    try:
        _obter_backend()
    except Exception:
        logger.info("Sem backend do computador nesta máquina.", exc_info=True)


def instrucoes() -> str:
    """As regras que valem para as duas ferramentas, mandadas uma vez na conexão."""
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "instrucoes.md").read_text(encoding="utf-8").strip()


class Cancelado(Exception):
    """A chamada foi interrompida pelo cliente ou pelo fim do turno."""


class _Turno:
    """Uma task que roda as chamadas em fila: o `computador` liga aprovação e
    moldura à task, então chamadas seguidas ficam no mesmo turno."""

    def __init__(self, ao_fechar: Callable[[], None], ocioso: float) -> None:
        self._fila: Optional[asyncio.Queue] = None
        self._tarefa: Optional[asyncio.Task] = None
        self._ao_fechar = ao_fechar
        self._ocioso = ocioso

    async def rodar(self, fabrica: Callable[[], Awaitable[Any]]) -> Any:
        if self._tarefa is None or self._tarefa.done():
            self._fila = asyncio.Queue()
            self._tarefa = asyncio.create_task(self._laco())
        futuro = asyncio.get_running_loop().create_future()
        await self._fila.put((fabrica, futuro))
        return await futuro

    def cancelar(self) -> None:
        if self._tarefa is not None and not self._tarefa.done():
            self._tarefa.cancel()

    async def fechar(self) -> None:
        self.cancelar()
        if self._tarefa is not None:
            try:
                await self._tarefa
            except asyncio.CancelledError:
                pass

    async def _laco(self) -> None:
        atual: Optional[asyncio.Future] = None
        try:
            while True:
                try:
                    fabrica, atual = await asyncio.wait_for(self._fila.get(), self._ocioso)
                except asyncio.TimeoutError:
                    return
                try:
                    atual.set_result(await fabrica())
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # o erro volta para quem chamou
                    atual.set_exception(exc)
                atual = None
        finally:
            # Interrompido no meio: a chamada em curso e as da fila recebem o aviso.
            pendentes = [atual] if atual is not None else []
            while self._fila is not None and not self._fila.empty():
                pendentes.append(self._fila.get_nowait()[1])
            for futuro in pendentes:
                if not futuro.done():
                    futuro.set_exception(Cancelado("interrompido"))
            self._ao_fechar()


class Servidor:
    def __init__(self, enviar: Callable[[dict], None], *, aprovacao_no_cliente: bool = False,
                 sessao: str = "tato-mcp", ocioso: float = OCIOSO_S) -> None:
        from ..estado import EstadoDoTurno

        self._enviar = enviar
        self._aprovacao_no_cliente = aprovacao_no_cliente
        self._pergunta_ao_cliente = False
        self._ids = itertools.count(1)
        self._pendentes: Dict[int, asyncio.Future] = {}
        self._pedidos: Dict[Any, asyncio.Task] = {}
        self.state = EstadoDoTurno.new(sessao, "mcp")
        self._turno = _Turno(self._fechar_turno, ocioso)

    async def fechar(self) -> None:
        """Fecha o turno aberto (a moldura sai), solta a aba do navegador e
        para a ponte antes de o servidor sair."""
        from ..navegador import ponte_local
        from ..gravacao_mcp import encerrar as encerrar_gravacao

        await self._turno.fechar()
        await asyncio.to_thread(encerrar_gravacao, self.state.session_id)
        await ponte_local.soltar(self.state.session_id)
        await ponte_local.fechar()

    # Protocolo
    async def receber(self, mensagem: dict) -> None:
        """Uma mensagem do cliente: pedido, notificação ou resposta a uma pergunta nossa."""
        if "method" not in mensagem:
            futuro = self._pendentes.pop(mensagem.get("id"), None)
            if futuro is not None and not futuro.done():
                futuro.set_result(mensagem)
            return
        ident, metodo = mensagem.get("id"), mensagem["method"]
        params = mensagem.get("params") or {}
        if ident is None:
            if metodo == "notifications/cancelled":
                self._cancelar(params.get("requestId"))
            return
        tarefa = asyncio.current_task()
        if tarefa is not None:
            self._pedidos[ident] = tarefa
        try:
            resultado = await self._atender(metodo, params)
            self._enviar({"jsonrpc": "2.0", "id": ident, "result": resultado})
        except asyncio.CancelledError:
            return  # cancelado pelo cliente: o protocolo pede que não haja resposta
        except _MetodoDesconhecido:
            self._enviar({"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": f"método {metodo}"}})
        except Exception as exc:
            logger.exception("Falha no pedido %s", metodo)
            self._enviar({"jsonrpc": "2.0", "id": ident, "error": {"code": -32603, "message": str(exc)[:300]}})
        finally:
            self._pedidos.pop(ident, None)

    def _cancelar(self, ident: Any) -> None:
        """O cliente desistiu: para a chamada como o atalho de parar."""
        tarefa = self._pedidos.pop(ident, None)
        if tarefa is None:
            return
        from ..computador.computador import parar_turno

        parar_turno(self.state.session_id)  # gesto numa thread também para
        self._turno.cancelar()
        tarefa.cancel()

    async def _atender(self, metodo: str, params: dict) -> dict:
        if metodo == "initialize":
            from ..computador.computador_moldura import nome_do_cliente, usar_agente

            self._pergunta_ao_cliente = "elicitation" in (params.get("capabilities") or {})
            usar_agente(nome_do_cliente((params.get("clientInfo") or {}).get("name", "")))
            from .. import __version__
            from ..navegador.ponte_local import preparar

            asyncio.get_running_loop().run_in_executor(None, aquecer)
            asyncio.ensure_future(preparar())
            return {"protocolVersion": params.get("protocolVersion") or _VERSAO,
                    "capabilities": {"tools": {}}, "serverInfo": {"name": "tato", "version": __version__},
                    "instructions": instrucoes()}
        if metodo == "tools/list":
            return {"tools": esquemas()}
        if metodo == "tools/call":
            token = (params.get("_meta") or {}).get("progressToken")
            return await self._chamar(str(params.get("name") or ""), dict(params.get("arguments") or {}), token)
        if metodo == "ping":
            return {}
        raise _MetodoDesconhecido(metodo)

    async def perguntar(self, mensagem: str) -> bool:
        """`elicitation/create` ao cliente: a pessoa aprova ou não, lá."""
        ident = next(self._ids)
        futuro = asyncio.get_running_loop().create_future()
        self._pendentes[ident] = futuro
        self._enviar({"jsonrpc": "2.0", "id": ident, "method": "elicitation/create", "params": {
            "message": mensagem,
            "requestedSchema": {"type": "object", "required": ["aprovar"], "properties": {
                "aprovar": {"type": "boolean", "title": "Aprovar", "description": "Deixar o Tato fazer isso agora"}}}}})
        try:
            resposta = await asyncio.wait_for(futuro, _PRAZO_DA_PERGUNTA_S)
        except asyncio.TimeoutError:
            self._pendentes.pop(ident, None)
            return False
        resultado = resposta.get("result") or {}
        logger.info("Resposta do cliente à aprovação: %s", json.dumps(resposta.get("result") or resposta.get("error"),
                                                                      ensure_ascii=False)[:300])
        return resultado.get("action") == "accept" and bool((resultado.get("content") or {}).get("aprovar"))

    # Progresso
    def _progresso(self, token: Any, passo: int, mensagem: str) -> None:
        if token is None or not mensagem:
            return
        self._enviar({"jsonrpc": "2.0", "method": "notifications/progress",
                      "params": {"progressToken": token, "progress": passo, "message": mensagem[:200]}})

    def _ouvinte(self, token: Any) -> Callable[[dict], None]:
        """Envia ao cliente eventos de progresso da ferramenta."""
        contador = itertools.count(2)

        def ouvir(evento: dict) -> None:
            mensagem = ""
            if evento.get("type") == "computador":
                if evento.get("fase") == "gesto":
                    mensagem = f"{evento.get('titulo') or evento.get('acao')}: {evento.get('status')}"
                elif evento.get("fase") == "sessao":
                    mensagem = f"computador {evento.get('status')}"
            elif evento.get("type") == "navegador" and evento.get("fase") in ("inicio", "fim"):
                mensagem = f"navegador {evento.get('fase')}" + (f" · {evento['site']}" if evento.get("site") else "")
            self._progresso(token, next(contador), mensagem)

        return ouvir

    # Ferramentas
    async def _chamar(self, nome: str, argumentos: dict, token: Any = None) -> dict:
        if nome not in FERRAMENTAS:
            return _conteudo({"ok": False, "erro": f"ferramenta desconhecida: {nome}"}, None)
        self._progresso(token, 1, f"{nome}: começando")
        from ..politica import portao

        decisao = portao.evaluate(self.state, nome, "", argumentos)
        if not decisao.allowed:
            return _conteudo({"ok": False, "executado": False, "erro": decisao.reason}, None)
        if nome == "gravacao" and argumentos.get("acao") == "iniciar":
            if not (self._pergunta_ao_cliente or self._aprovacao_no_cliente):
                return _conteudo({"ok": False, "executado": False, "erro": (
                    "gravar a tela exige aprovação da pessoa; este cliente não oferece elicitation. "
                    "Use um cliente que peça aprovação por ferramenta e registre com --aprovacao-no-cliente")}, None)
            if not await self._aprovado("gravacao iniciar", "gravar a tela inteira localmente, até 15 minutos"):
                return _conteudo({"ok": False, "executado": False, "recusado": True,
                                  "erro": "a pessoa não aprovou a gravação da tela"}, None)
        try:
            resultado = await self._turno.rodar(lambda: self._executar(nome, argumentos, token))
            if isinstance(resultado, dict) and resultado.get("precisa_permissao"):
                comando = str(resultado.get("comando") or nome)
                if not (self._pergunta_ao_cliente or self._aprovacao_no_cliente):
                    resultado = {"ok": False, "executado": False, "recusado": True, "erro": (
                        f"nada foi executado: {comando} precisa da aprovação da pessoa, e este cliente não "
                        "sabe perguntar (sem elicitation). Se ele já pede permissão antes de cada ferramenta, "
                        "registre o Tato com --aprovacao-no-cliente")}
                elif await self._aprovado(comando, str(resultado.get("motivo") or "")):
                    from ..permissoes import responder

                    responder(comando, "uma_vez", self.state.session_id)
                    resultado = await self._turno.rodar(lambda: self._executar(nome, argumentos, token))
                else:
                    resultado = {"ok": False, "executado": False, "recusado": True,
                                 "erro": f"a pessoa não aprovou: {comando}"}
        except Cancelado:
            resultado = {"ok": False, "executado": False, "interrompido": True,
                         "erro": "interrompido: nenhum gesto roda depois disso, e a moldura saiu"}
        tela = self._tirar_print()
        if tela and isinstance(resultado, dict):
            # A captura acompanha o resultado MCP atual.
            resultado = {**resultado, "print_anexado": "a imagem da tela está neste resultado"}
        return _conteudo(resultado, tela)

    async def _aprovado(self, comando: str, motivo: str) -> bool:
        # Quem registrou com --aprovacao-no-cliente disse que o cliente já pergunta
        # antes de cada chamada: essa pergunta vale, sem uma segunda por cima.
        if self._aprovacao_no_cliente:
            return True
        if self._pergunta_ao_cliente:
            return await self.perguntar(f"O Tato quer {motivo or 'fazer isto'}: {comando}. Aprovar?")
        return False

    async def _executar(self, nome: str, argumentos: dict, token: Any = None) -> Any:
        """Direto na ferramenta, com o prazo dela; os eventos viram progresso."""
        from ..emissor import EMISSOR
        from ..prazo import com_prazo

        marca = EMISSOR.set(self._ouvinte(token))
        try:
            if nome == "computador":
                from ..computador.computador import executar

                return await com_prazo(nome, executar(argumentos, self.state), argumentos)
            if nome == "gravacao":
                from ..gravacao_mcp import executar

                return await com_prazo(nome, executar(argumentos, self.state), argumentos)
            from ..navegador.browser_contracts import Action
            from ..navegador.browser_registry import despachar

            try:
                action = Action.de_argumentos(argumentos)
            except ValueError as exc:
                return {"ok": False, "executado": False, "fase": "contrato", "motivo": str(exc)}
            return await com_prazo(nome, despachar(
                action, self.state, provider_name=str(argumentos.get("provider") or "").strip() or None))
        finally:
            EMISSOR.reset(marca)

    def _tirar_print(self) -> Optional[dict]:
        tela = getattr(self.state, "print_da_tela", None)
        self.state.print_da_tela = None
        return tela if isinstance(tela, dict) and str(tela.get("data_url") or "").startswith("data:image/") else None

    def _fechar_turno(self) -> None:
        """Fim do turno: a moldura sai e, se a pessoa estava com a vez, ela volta
        ao agente. O próximo gesto abre outro turno e pede aprovação de novo."""
        from ..computador import computador_vez
        from ..computador.computador import encerrar_do_turno

        try:
            encerrar_do_turno(self.state)
        except Exception:
            logger.warning("Não consegui fechar o turno do computador.", exc_info=True)
        computador_vez.devolver()


class _MetodoDesconhecido(Exception):
    pass


def _conteudo(resultado: Any, tela: Optional[dict]) -> dict:
    """O resultado da ferramenta como texto; o print, como imagem de verdade."""
    falhou = isinstance(resultado, dict) and (resultado.get("ok") is False or "erro" in resultado)
    partes = [{"type": "text", "text": json.dumps(resultado, ensure_ascii=False, default=str)}]
    if tela:
        cabecalho, dados = tela["data_url"].split("base64,", 1)
        partes.append({"type": "image", "data": dados, "mimeType": cabecalho[5:].rstrip(";") or "image/png"})
    return {"content": partes, "isError": bool(falhou)}


def _stdio_em_utf8(entrada, saida) -> None:
    """Em pipe, o Python do Windows usa a página de código regional; o protocolo é UTF-8."""
    entrada.reconfigure(encoding="utf-8", errors="replace")
    saida.reconfigure(encoding="utf-8", newline="\n")


async def _principal(aprovacao_no_cliente: bool) -> None:
    _stdio_em_utf8(sys.stdin, sys.stdout)
    saida = sys.stdout
    # O stdout é do protocolo: qualquer print perdido vai para o stderr.
    sys.stdout = sys.stderr
    loop = asyncio.get_running_loop()
    entrada: asyncio.Queue = asyncio.Queue()

    def ler() -> None:
        for linha in sys.stdin:
            loop.call_soon_threadsafe(entrada.put_nowait, linha)
        loop.call_soon_threadsafe(entrada.put_nowait, None)

    def enviar(mensagem: dict) -> None:
        saida.write(json.dumps(mensagem, ensure_ascii=False) + "\n")
        saida.flush()

    # Uma sessão por processo: dois agentes com o Tato não dividem a mesma aba.
    servidor = Servidor(enviar, aprovacao_no_cliente=aprovacao_no_cliente, sessao=f"tato-{os.getpid()}")
    threading.Thread(target=ler, daemon=True).start()
    tarefas = set()
    while (linha := await entrada.get()) is not None:
        try:
            mensagem = json.loads(linha)
        except ValueError:
            continue
        # Cada pedido na sua task: uma chamada pode esperar a aprovação da pessoa.
        tarefa = asyncio.create_task(servidor.receber(mensagem))
        tarefas.add(tarefa)
        tarefa.add_done_callback(tarefas.discard)
    # O cliente fechou a entrada: o que está em curso termina e responde antes.
    if tarefas:
        await asyncio.gather(*tarefas, return_exceptions=True)
    await servidor.fechar()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(_principal("--aprovacao-no-cliente" in sys.argv[1:]))


if __name__ == "__main__":
    main()
