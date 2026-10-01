"""Uma chamada que não volta não pode travar o agente.

O prazo é cooperativo: a espera é abandonada e o agente recebe um erro que ele
lê, em vez de uma exceção. Um gesto síncrono num executor termina sozinho.
Cada ferramenta declara o seu `prazo_s` em `ferramentas.json`.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Dict

logger = logging.getLogger(__name__)

PRAZO_PADRAO_S = 45.0


@functools.lru_cache(maxsize=1)
def _declarados() -> Dict[str, Dict[str, float]]:
    try:
        ferramentas = json.loads((Path(__file__).with_name("ferramentas.json")).read_text(encoding="utf-8"))
        return {f["name"]: {"prazo_s": float(f.get("prazo_s") or 0),
                            "prazo_por_caractere_s": float(f.get("prazo_por_caractere_s") or 0)}
                for f in ferramentas}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def prazo_de(tool: str, argumentos: Any = None) -> float:
    """Prazo declarado, mais `prazo_por_caractere_s` por caractere de `texto` ou `valor`."""
    meta = _declarados().get(tool, {})
    valor = meta.get("prazo_s") or PRAZO_PADRAO_S
    por_caractere = meta.get("prazo_por_caractere_s") or 0.0
    if por_caractere > 0 and isinstance(argumentos, dict):
        texto = argumentos.get("texto") or argumentos.get("valor") or ""
        valor += por_caractere * len(str(texto))
    return valor


def estouro(tool: str, segundos: float) -> Dict[str, Any]:
    return {
        "erro": (f"a ferramenta '{tool}' não respondeu em {segundos:.0f}s e foi abandonada. "
                 "Tente outro caminho, ou responda com o que já tem dizendo o que faltou."),
        "timeout": True,
    }


async def com_prazo(tool: str, chamada: Awaitable[Any], argumentos: Any = None) -> Any:
    segundos = prazo_de(tool, argumentos)
    try:
        return await asyncio.wait_for(chamada, timeout=segundos)
    except asyncio.TimeoutError:
        logger.warning("Ferramenta %s não respondeu em %.0fs; abandonando a espera.", tool, segundos)
        return estouro(tool, segundos)


__all__ = ["PRAZO_PADRAO_S", "com_prazo", "estouro", "prazo_de"]
