"""Contrato de ciclo de vida de um provedor de navegador."""
from __future__ import annotations

from abc import ABC, abstractmethod

from .browser_contracts import BrowserCapability


class BrowserProvider(ABC):
    """Cria sessões sem vazar detalhes do backend para o despachante."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Identificador estável usado na configuração e no registro."""

    @abstractmethod
    def is_available(self) -> bool:
        """Consulta barata, sem rede, sobre a disponibilidade local."""

    @abstractmethod
    def create_session(self, task_id: str) -> BrowserCapability:
        """Cria ou conecta uma sessão para a tarefa."""

    @abstractmethod
    def close_session(self, session_id: str) -> bool:
        """Fecha uma sessão conhecida sem interromper outras."""

    @abstractmethod
    def emergency_cleanup(self, session_id: str) -> None:
        """Tenta encerrar a sessão durante uma saída inesperada."""


__all__ = ["BrowserProvider"]
