"""O estado de uma sessão do servidor: quem é e o último print, que vai como
imagem na resposta seguinte."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EstadoDoTurno:
    session_id: str
    user_id: str = ""
    funcoes_ativas: List[str] = field(default_factory=lambda: ["computador", "browser"])
    print_da_tela: Optional[Dict[str, Any]] = None
    processo_do_turno: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def new(cls, session_id: str, user_id: str = "") -> "EstadoDoTurno":
        return cls(session_id=session_id, user_id=user_id)


__all__ = ["EstadoDoTurno"]
