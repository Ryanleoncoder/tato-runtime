"""As abas do Chrome para um processo que não hospeda a ponte.

A extensão conversa com um servidor só, a API local. O servidor MCP do Tato
roda em outro processo: manda os comandos para a API, que os entrega à
extensão. A política e o anti-SSRF rodam antes, no Tato; a API confere de novo
a URL de navegação e o segredo local.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

from .browser_chrome_bridge import segredo_do_tato
from .browser_chrome_provider import ChromeTabProvider, ChromeTabSession

logger = logging.getLogger(__name__)

_ROTA = "/tato/comando"
_PRAZO_S = 45.0


def _base() -> str:
    from .ponte_local import porta

    return f"http://127.0.0.1:{porta()}"


class PonteRemota:
    def __init__(self) -> None:
        self._visto_em = 0.0
        self._ligada = False

    def _pedido(self, comando: str, conversa: str, argumentos: dict[str, Any]) -> dict[str, Any]:
        from ..computador.computador_moldura import agente

        return {"comando": comando, "conversa": conversa, "argumentos": argumentos, "agente": agente()}

    def _cabecalho(self) -> dict[str, str]:
        return {"X-Tato-Segredo": segredo_do_tato()}

    async def chamar(self, comando: str, conversa: str, **argumentos: Any) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=_PRAZO_S) as cliente:
            try:
                resposta = await cliente.post(_base() + _ROTA, json=self._pedido(comando, conversa, argumentos),
                                              headers=self._cabecalho())
            except httpx.HTTPError as exc:
                self._ligada = False
                raise ConnectionError(f"a API local do navegador não respondeu: {exc}") from exc
        if resposta.status_code == 401:
            raise PermissionError("a API recusou o segredo do Tato; reinicie a API para ela ler o mesmo arquivo")
        resposta.raise_for_status()
        corpo = resposta.json()
        if not corpo.get("ok"):
            raise RuntimeError(str(corpo.get("erro") or "comando recusado"))
        return dict(corpo.get("resultado") or {})

    def disponivel(self) -> bool:
        """A API respondeu há pouco. A pergunta é barata e fica guardada 5 s."""
        agora = time.monotonic()
        if agora - self._visto_em < 5:
            return self._ligada
        self._visto_em = agora
        try:
            resposta = httpx.post(_base() + _ROTA, json={"comando": "show", "conversa": "tato-sonda"},
                                  headers=self._cabecalho(), timeout=1.0)
            corpo = resposta.json() if resposta.status_code == 200 else {}
            self._ligada = resposta.status_code == 200 and "não conectada" not in str(corpo.get("erro") or "")
        except (httpx.HTTPError, ValueError):
            self._ligada = False
        return self._ligada

    def sem_esperar(self, comando: str, conversa: str) -> bool:
        try:
            httpx.post(_base() + _ROTA, json=self._pedido(comando, conversa, {}), headers=self._cabecalho(),
                       timeout=2.0)
            return True
        except httpx.HTTPError:
            logger.debug("Comando %s sem resposta da API.", comando, exc_info=True)
            return False


class ChromeRemotoProvider(ChromeTabProvider):
    """O mesmo `chrome_tabs`, com a ponte do outro lado da API."""

    def __init__(self, ponte: PonteRemota | None = None) -> None:
        self._ponte = ponte or PonteRemota()

    def is_available(self) -> bool:
        return self._ponte.disponivel()

    def create_session(self, task_id: str) -> ChromeTabSession:
        if not self.is_available():
            raise ConnectionError("a API local ou a extensão do Chrome não estão conectadas")
        return ChromeTabSession(task_id, chamar=self._ponte.chamar)

    def close_session(self, session_id: str) -> bool:
        return self._ponte.sem_esperar("close", str(session_id).removeprefix("chrome-tabs:"))


__all__ = ["ChromeRemotoProvider", "PonteRemota"]
