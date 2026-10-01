"""O canal de eventos da chamada em curso, alcançável de dentro das ferramentas.

Um roteiro roda vários passos numa chamada só e avisa cada um. O ContextVar
leva o ouvinte sem passar callback por parâmetro, e vale só na chamada que o
definiu.
"""
from __future__ import annotations

import contextvars
from typing import Any, Callable, Dict, Optional

EMISSOR: contextvars.ContextVar[Optional[Callable[[Dict[str, Any]], None]]] = \
    contextvars.ContextVar("tato_emissor", default=None)


def emitir(evento: Dict[str, Any]) -> None:
    """Manda o evento a quem ouve, se houver. Nunca levanta: o aviso não pode
    derrubar a ação que ele anuncia."""
    fn = EMISSOR.get()
    if fn is None:
        return
    try:
        fn(evento)
    except Exception:
        pass


__all__ = ["EMISSOR", "emitir"]
