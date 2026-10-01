"""O texto do balão que a extensão mostra sobre a aba controlada.

A borda, a seta e o balão são desenhados pela extensão (`extensao/browser_sobreposicao.js`);
daqui sai só o texto de cada ação.
"""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import urlparse


def _curto(texto: Any, limite: int = 48) -> str:
    texto = " ".join(str(texto or "").split())
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


def rotulo(tipo: str, argumentos: Mapping[str, Any], nome_do_alvo: str = "") -> str:
    """O texto do balão. Em `digitar` vai o CAMPO, nunca o que é digitado:
    senha e chave ficariam na tela de quem estiver olhando."""
    if tipo == "navegar":
        host = urlparse(str(argumentos.get("url") or "")).netloc
        return f"navegar · {host}" if host else "navegar"
    if tipo == "clicar":
        return f"clicar · “{_curto(nome_do_alvo)}”" if nome_do_alvo else "clicar"
    if tipo == "digitar":
        return f"digitar em “{_curto(nome_do_alvo)}”" if nome_do_alvo else "digitar"
    if tipo == "rolar":
        return "rolar"
    return tipo


__all__ = ["rotulo"]
