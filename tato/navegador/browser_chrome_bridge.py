"""Ponte local entre o servidor e a extensão de abas próprias do Chrome.

O websocket só aceita uma extensão local emparelhada. Não recebe ``tab_id``
do agente: cada comando leva apenas o identificador da conversa; a extensão
resolve esse identificador no registro das abas que ela mesma criou.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from starlette.websockets import WebSocket
from starlette.websockets import WebSocketDisconnect

from . import web


_ORIGEM = re.compile(r"^chrome-extension://[a-p]{32}$")
# O ID fixo da extensão (a `key` do manifest). Ela se conecta sem código; outra
# extensão, ou uma cópia com outro ID, ainda precisa do código de conexão.
ID_DA_EXTENSAO = "kagbdhkhbjcaablmjcmcobkbhphfefmb"
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

logger = logging.getLogger(__name__)


def _arquivo_do_pareamento() -> Path:
    from ..config import pasta_do_tato

    return pasta_do_tato() / "navegador_extensao.json"


def segredo_do_tato() -> str:
    """O segredo que um Tato mostra para mandar comandos à ponte aberta por
    outro. Fica num arquivo da pessoa: só processo dela o lê."""
    from ..config import pasta_do_tato

    arquivo = pasta_do_tato() / "ponte.segredo"
    try:
        guardado = arquivo.read_text(encoding="utf-8").strip()
    except OSError:
        guardado = ""
    if guardado:
        return guardado
    novo = secrets.token_urlsafe(32)
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    arquivo.write_text(novo, encoding="utf-8")
    return novo


# Comandos permitidos para o cliente remoto. `reset` fica reservado ao servidor.
_COMANDOS_DO_TATO = frozenset({"execute", "show", "soltar", "close"})


def _resumo(segredo: str) -> str:
    return hashlib.sha256(segredo.encode("utf-8")).hexdigest()


class PonteChrome:
    """O pareamento sobrevive ao reinício da API: fica no disco o hash do token
    que a extensão guardou, nunca o token."""

    def __init__(self, arquivo: Optional[Path] = None) -> None:
        self._arquivo = arquivo
        self._socket: WebSocket | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pendentes: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._codigo = ""
        self._vence = 0.0
        self._token = ""
        self._resumo_salvo: Optional[str] = None
        self._fechadas: set[str] = set()

    @property
    def conectada(self) -> bool:
        return self._socket is not None and self._loop is not None and self._loop.is_running()

    def aba_aberta(self, conversa: str) -> bool:
        return self.conectada and conversa not in self._fechadas

    def codigo_de_pareamento(self) -> dict[str, Any]:
        """Código de uso único, exibido apenas ao cliente local autenticado."""
        self._codigo = secrets.token_urlsafe(12)
        self._vence = time.monotonic() + 300
        return {"codigo": self._codigo, "expira_em_segundos": 300}

    def _caminho(self) -> Path:
        return self._arquivo or _arquivo_do_pareamento()

    def _resumo_do_disco(self) -> str:
        if self._resumo_salvo is None:
            try:
                self._resumo_salvo = str(json.loads(self._caminho().read_text(encoding="utf-8")).get("sha256") or "")
            except (OSError, ValueError):
                self._resumo_salvo = ""
        return self._resumo_salvo

    def _salvar(self, token: str) -> None:
        self._resumo_salvo = _resumo(token)
        try:
            caminho = self._caminho()
            caminho.parent.mkdir(parents=True, exist_ok=True)
            caminho.write_text(json.dumps({"sha256": self._resumo_salvo}), encoding="utf-8")
        except OSError:
            logger.warning("Não consegui guardar o pareamento da extensão; ele vale até a API reiniciar.",
                           exc_info=True)

    def _autorizada(self, segredo: str, origem: str = "") -> bool:
        if not segredo and origem == f"chrome-extension://{ID_DA_EXTENSAO}":
            self._token = secrets.token_urlsafe(32)
            self._salvar(self._token)
            return True
        if self._codigo and time.monotonic() < self._vence and hmac.compare_digest(segredo, self._codigo):
            self._codigo = ""
            self._vence = 0.0
            self._token = secrets.token_urlsafe(32)
            self._salvar(self._token)
            return True
        if self._token and hmac.compare_digest(segredo, self._token):
            return True
        salvo = self._resumo_do_disco()
        if segredo and salvo and hmac.compare_digest(_resumo(segredo), salvo):
            self._token = segredo
            return True
        return False

    async def atender(self, socket: WebSocket) -> None:
        origem = socket.headers.get("origin", "")
        cliente = socket.client.host if socket.client else ""
        if not _ORIGEM.fullmatch(origem) or cliente not in _LOOPBACK:
            await socket.close(code=1008)
            return
        await socket.accept()
        try:
            primeiro = await asyncio.wait_for(socket.receive_json(), timeout=10)
            if not isinstance(primeiro, dict) or primeiro.get("kind") != "pair" \
                    or not self._autorizada(str(primeiro.get("secret") or ""), origem):
                await socket.close(code=1008)
                return
            anterior = self._socket
            if anterior is not None and anterior is not socket:
                await anterior.close(code=1001)
            self._socket = socket
            self._loop = asyncio.get_running_loop()
            await socket.send_json({"kind": "ready", "token": self._token})
            while True:
                mensagem = await socket.receive_json()
                if not isinstance(mensagem, dict):
                    continue
                if mensagem.get("kind") == "result":
                    identificador = str(mensagem.get("id") or "")
                    futuro = self._pendentes.pop(identificador, None)
                    if futuro is not None and not futuro.done():
                        futuro.set_result(mensagem)
                elif mensagem.get("kind") == "closed":
                    self._fechadas.add(str(mensagem.get("conversation") or ""))
                elif mensagem.get("kind") == "validate":
                    await self._validar_navegacao(socket, mensagem)
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        finally:
            if self._socket is socket:
                self._socket = None
                self._loop = None
                for futuro in self._pendentes.values():
                    if not futuro.done():
                        futuro.set_exception(ConnectionError("extensão do Chrome desconectada"))
                self._pendentes.clear()

    async def _validar_navegacao(self, socket: WebSocket, mensagem: dict[str, Any]) -> None:
        identificador = str(mensagem.get("id") or "")
        url = str(mensagem.get("url") or "")
        try:
            await asyncio.to_thread(web.validar_url, url)
            permitido, motivo = True, ""
        except Exception as exc:
            permitido, motivo = False, str(exc)[:240]
        await socket.send_json({"kind": "validation", "id": identificador,
                                "allowed": permitido, "reason": motivo})

    async def pelo_tato(self, pedido: dict[str, Any]) -> dict[str, Any]:
        """Recebe um comando remoto e o restringe às abas da sessão identificada."""
        comando = str(pedido.get("comando") or "")
        if comando not in _COMANDOS_DO_TATO:
            return {"ok": False, "erro": f"comando fora do que o Tato pode pedir: {comando or 'vazio'}"}
        conversa = str(pedido.get("conversa") or "").strip()[:120]
        if not conversa:
            return {"ok": False, "erro": "conversa vazia"}
        conversa = conversa if conversa.startswith("tato-") else f"tato-{conversa}"
        if not self.conectada:
            return {"ok": False, "erro": "extensão do Chrome não conectada"}
        argumentos = dict(pedido.get("argumentos") or {})
        try:
            acao = argumentos.get("action") or {}
            if comando == "execute" and acao.get("acao") == "navegar":
                web.validar_url(str((acao.get("argumentos") or {}).get("url") or ""))
            quem = str(pedido.get("agente") or "")[:40] or None
            try:
                resultado = await self.chamar(comando, conversa, quem=quem, **argumentos)
            except RuntimeError as exc:
                # Pelo MCP não há cartão para reabrir a aba fechada: abre outra e segue.
                if comando != "execute" or "foi fechada" not in str(exc):
                    raise
                await self.chamar("reset", conversa, quem=quem)
                resultado = await self.chamar(comando, conversa, quem=quem, **argumentos)
        except Exception as exc:
            return {"ok": False, "erro": str(exc)[:400]}
        return {"ok": True, "resultado": resultado}

    async def chamar(self, comando: str, conversa: str, *, quem: Optional[str] = None,
                     **argumentos: Any) -> dict[str, Any]:
        socket = self._socket
        if socket is None:
            raise ConnectionError("extensão do Chrome não conectada")
        identificador = secrets.token_urlsafe(12)
        futuro: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pendentes[identificador] = futuro
        from ..computador.computador_moldura import agente

        try:
            # `agente`: quem está usando, para o grupo de abas e a cor da sobreposição.
            await socket.send_json({"kind": "command", "id": identificador,
                                    "command": comando, "conversation": conversa,
                                    "arguments": argumentos, "agente": quem or agente()})
            resposta = await asyncio.wait_for(futuro, timeout=40)
            if not resposta.get("ok"):
                raise RuntimeError(str(resposta.get("error") or "comando recusado pela extensão"))
            if comando == "reset":
                self._fechadas.discard(conversa)
            return dict(resposta.get("result") or {})
        finally:
            self._pendentes.pop(identificador, None)

    def enviar_sem_esperar(self, comando: str, conversa: str) -> bool:
        loop = self._loop
        if not self.conectada or loop is None:
            return False
        try:
            atual = asyncio.get_running_loop()
        except RuntimeError:
            atual = None
        tarefa = self.chamar(comando, conversa)
        if atual is loop:
            loop.create_task(tarefa)
        else:
            asyncio.run_coroutine_threadsafe(tarefa, loop)
        return True


ponte_chrome = PonteChrome()


__all__ = ["PonteChrome", "ponte_chrome"]
