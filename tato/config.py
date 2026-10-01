"""Configuração por variável de ambiente `TATO_*`.

Quem lê usa `getattr(settings, "TATO_X", padrao)`: variável ausente cai no
padrão de quem pergunta.
"""
from __future__ import annotations

import os
from pathlib import Path


class _Configuracao:
    def __getattr__(self, nome: str) -> str:
        if nome.startswith("TATO_") and nome in os.environ:
            return os.environ[nome]
        raise AttributeError(nome)


settings = _Configuracao()


def pasta_do_tato() -> Path:
    """Onde o Tato guarda o que precisa sobreviver entre execuções."""
    return Path(os.environ.get("TATO_HOME") or Path.home() / ".tato")


__all__ = ["pasta_do_tato", "settings"]
