"""Opera abas próprias no Chrome da pessoa por meio da extensão local."""
from __future__ import annotations

from . import web
from .browser_chrome_bridge import ponte_chrome
from .browser_contracts import Action, BrowserCapability, Observation
from .browser_provider import BrowserProvider


class ChromeTabSession(BrowserCapability):
    """`chamar` manda o comando à extensão: a ponte deste processo, ou a da API
    quando quem usa é o servidor MCP do Tato."""

    def __init__(self, conversa: str, chamar=None) -> None:
        self.conversa = conversa
        self.session_id = f"chrome-tabs:{conversa}"
        self._chamar = chamar

    async def _comando(self, comando: str, **argumentos):
        if self._chamar is not None:
            return await self._chamar(comando, self.conversa, **argumentos)
        return await ponte_chrome.chamar(comando, self.conversa, **argumentos)

    def esta_viva(self) -> bool:
        return self._chamar is not None or ponte_chrome.aba_aberta(self.conversa)

    async def execute(self, action: Action) -> Observation:
        if action.tipo == "navegar":
            # A extensão valida cada requisição de novo, inclusive redirects.
            web.validar_url(str(action.argumentos.get("url") or ""))
        resultado = await self._comando("execute", action=action.para_dict())
        return Observation(
            fonte=str(resultado.get("fonte") or "chrome_tab"),
            dados=resultado.get("dados"),
            epoch=int(resultado.get("epoch") or 0),
            confianca=resultado.get("confianca"),
            interativa=bool(resultado.get("interativa")),
            metadados=dict(resultado.get("metadados") or {}),
        )

    async def trazer_para_frente(self) -> bool:
        resultado = await self._comando("show")
        return bool(resultado.get("shown"))


class ChromeTabProvider(BrowserProvider):
    @property
    def name(self) -> str:
        return "chrome_tabs"

    def is_available(self) -> bool:
        return ponte_chrome.conectada

    def create_session(self, task_id: str) -> ChromeTabSession:
        if not self.is_available():
            raise ConnectionError("extensão de abas do Chrome não conectada")
        return ChromeTabSession(task_id)

    def close_session(self, session_id: str) -> bool:
        conversa = str(session_id).removeprefix("chrome-tabs:")
        return ponte_chrome.enviar_sem_esperar("close", conversa)

    def emergency_cleanup(self, session_id: str) -> None:
        # Desconectar o servidor não precisa fechar a aba que é do usuário.
        # A extensão a mantém visível; o vínculo pode ser refeito depois.
        return None


def soltar_do_turno(state) -> bool:
    """Fim do turno que usou o navegador: a borda e a seta da aba apagam. Elas
    ficam acesas o turno inteiro, inclusive enquanto o modelo pensa."""
    conversa = str(getattr(state, "session_id", "") or "")
    if not conversa or not getattr(state, "navegador_do_turno", None) or not ponte_chrome.aba_aberta(conversa):
        return False
    return ponte_chrome.enviar_sem_esperar("soltar", conversa)


__all__ = ["ChromeTabProvider", "ChromeTabSession", "soltar_do_turno"]
