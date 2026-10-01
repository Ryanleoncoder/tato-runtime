"""Controla a alternância entre agente e pessoa no computador.

A pessoa assume passos privados e devolve o controle quando terminar.
Enquanto ela controla a tela, ações e leituras do agente são recusadas.
A troca invalida observações iniciadas na vez anterior.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass
from typing import Dict

AGENTE = "agente"
PESSOA = "pessoa"


@dataclass(frozen=True)
class Vez:
    com: str = AGENTE
    sessao: str = ""
    motivo: str = ""
    desde: float = 0.0
    epoca: int = 0

    @property
    def da_pessoa(self) -> bool:
        return self.com == PESSOA


_trava = threading.Lock()
_vez = Vez()


def atual() -> Vez:
    with _trava:
        return _vez


def passar_para_pessoa(sessao: str, motivo: str) -> Vez:
    """A vez vai para a pessoa. Se já era dela, só o motivo muda."""
    global _vez
    motivo = " ".join(str(motivo or "").split())[:160]
    with _trava:
        epoca = _vez.epoca if _vez.da_pessoa else _vez.epoca + 1
        _vez = Vez(PESSOA, sessao, motivo, time.time(), epoca)
        return _vez


def devolver() -> Vez:
    """A vez volta para o agente. Devolver o que já era dele não muda nada."""
    global _vez
    with _trava:
        if _vez.da_pessoa:
            _vez = Vez(AGENTE, "", "", time.time(), _vez.epoca + 1)
        return _vez


def publica(vez: Vez) -> Dict[str, object]:
    return asdict(vez)


def _limpar_para_testes() -> None:
    global _vez
    with _trava:
        _vez = Vez()


__all__ = ["AGENTE", "PESSOA", "Vez", "atual", "devolver", "passar_para_pessoa", "publica"]
