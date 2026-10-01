"""A porta onde a extensão do Chrome encontra o Tato.

O primeiro Tato que sobe abre `127.0.0.1:TATO_PORTA` (47812): a extensão se
conecta ali (`/tato/ws`) e os outros Tato mandam seus comandos pelo mesmo lugar
(`/tato/comando`, com o segredo local). Quem acha a porta ocupada vira cliente
desse primeiro: dois agentes usam a mesma extensão, cada um na sua aba.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import socket
from typing import Optional

logger = logging.getLogger(__name__)

PORTA_PADRAO = 47812
_servidor = None  # o uvicorn deste processo, quando é ele quem abriu a porta
_tarefa: Optional[asyncio.Future] = None


def porta() -> int:
    try:
        return int(os.environ.get("TATO_PORTA") or PORTA_PADRAO)
    except ValueError:
        return PORTA_PADRAO


def _abrir_porta() -> Optional[socket.socket]:
    """O socket já escutando, ou None quando outro processo tem a porta."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        # No Windows, SO_EXCLUSIVEADDRUSE impede dois processos na mesma porta.
        sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", porta()))
        sock.listen(16)
    except OSError:
        sock.close()
        return None
    sock.setblocking(False)
    return sock


def aplicativo():
    """As duas rotas: a da extensão e a dos outros Tato."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute

    from .browser_chrome_bridge import ponte_chrome, segredo_do_tato

    async def extensao(websocket) -> None:
        await ponte_chrome.atender(websocket)

    async def comando(request: Request) -> JSONResponse:
        recebido = request.headers.get("x-tato-segredo") or ""
        if not recebido or not hmac.compare_digest(recebido, segredo_do_tato()):
            return JSONResponse({"detail": "segredo do Tato inválido"}, status_code=401)
        try:
            pedido = await request.json()
        except ValueError:
            return JSONResponse({"detail": "corpo inválido"}, status_code=400)
        return JSONResponse(await ponte_chrome.pelo_tato(pedido if isinstance(pedido, dict) else {}))

    return Starlette(routes=[WebSocketRoute("/tato/ws", extensao),
                             Route("/tato/comando", comando, methods=["POST"])])


async def preparar() -> str:
    """Abre a porta ou se liga a quem a tem, e registra o provedor do Chrome.
    Devolve "ponte" ou "cliente"."""
    global _servidor, _tarefa
    from . import browser_registry
    from .browser_chrome_provider import ChromeTabProvider
    from .browser_chrome_remoto import ChromeRemotoProvider

    if any(p.name == "chrome_tabs" for p in browser_registry.listar()):
        return "ponte" if _servidor is not None else "cliente"
    sock = _abrir_porta()
    if sock is None:
        browser_registry.registrar(ChromeRemotoProvider())
        logger.info("Porta %s com outro Tato: comandos do navegador vão por ele.", porta())
        return "cliente"
    import uvicorn

    # Sem log no stdout: ele é o canal do MCP.
    config = uvicorn.Config(aplicativo(), log_config=None, access_log=False, lifespan="off")
    _servidor = uvicorn.Server(config)
    _tarefa = asyncio.ensure_future(_servidor.serve(sockets=[sock]))
    browser_registry.registrar(ChromeTabProvider())
    logger.info("Ponte do navegador em 127.0.0.1:%s.", porta())
    return "ponte"


async def soltar(conversa: str) -> None:
    """Fim do processo: a aba da conversa apaga a borda e a seta, e a página
    deixa de dizer que o agente está pensando."""
    try:
        if _servidor is not None:
            from .browser_chrome_bridge import ponte_chrome

            if ponte_chrome.conectada and ponte_chrome.aba_aberta(conversa):
                await asyncio.wait_for(ponte_chrome.chamar("soltar", conversa), 3)
        else:
            from .browser_chrome_remoto import PonteRemota

            await asyncio.wait_for(PonteRemota().chamar("soltar", conversa), 3)
    except Exception:
        logger.debug("A aba não foi solta no fim.", exc_info=True)


async def fechar() -> None:
    """Para a ponte esperando as conexões abertas fecharem."""
    global _servidor, _tarefa
    if _servidor is None:
        return
    _servidor.should_exit = True
    try:
        await asyncio.wait_for(asyncio.shield(_tarefa), 5)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        logger.debug("A ponte não fechou no prazo.", exc_info=True)
    _servidor, _tarefa = None, None


__all__ = ["PORTA_PADRAO", "aplicativo", "fechar", "porta", "preparar", "soltar"]
