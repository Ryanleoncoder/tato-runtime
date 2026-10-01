"""As aprovações que a pessoa deu, por sessão, enquanto o servidor roda.

A chave é o primeiro termo do comando (`computador`, `browser_clicar`).
`uma_vez` vale para a próxima execução e some; `sessao` vale até a sessão
acabar. Ação que se confirma sempre na hora (`na_hora`: candidatura, envio de
dado) só passa com o crédito de `uma_vez`.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict

RESPOSTAS = ("uma_vez", "sessao", "negar")

_TRAVA = threading.RLock()
_DA_SESSAO: Dict[str, Dict[str, float]] = {}
_UMA_VEZ: Dict[str, Dict[str, float]] = {}


def assinatura(comando: str) -> str:
    partes = str(comando or "").split()
    return partes[0].lower() if partes else ""


def responder(comando: str, resposta: str, session_id: str = "") -> Dict[str, Any]:
    """Registra a decisão de quem foi perguntado."""
    chave = assinatura(comando)
    escolha = str(resposta or "").strip().lower()
    if not chave:
        raise ValueError("comando vazio")
    if escolha not in RESPOSTAS:
        raise ValueError(f"resposta deve ser uma de {', '.join(RESPOSTAS)}")
    sessao = session_id or "global"
    with _TRAVA:
        if escolha == "uma_vez":
            _UMA_VEZ.setdefault(sessao, {})[chave] = time.time()
        elif escolha == "sessao":
            _DA_SESSAO.setdefault(sessao, {})[chave] = time.time()
    return {"chave": chave, "resposta": escolha}


def avaliar(comando: str, session_id: str = "", *, consumir: bool = True,
            na_hora: bool = False) -> Dict[str, Any]:
    """Se o comando pode rodar agora. `precisa_perguntar` é quando a pessoa
    decide; `consumir=False` só consulta, sem gastar o crédito de `uma_vez`."""
    chave = assinatura(comando)
    sessao = session_id or "global"
    with _TRAVA:
        if chave in _DA_SESSAO.get(sessao, {}) and not na_hora:
            return {"permitido": True, "precisa_perguntar": False, "chave": chave,
                    "motivo": f"'{chave}' foi autorizado nesta sessão"}
        creditos = _UMA_VEZ.get(sessao, {})
        if chave in creditos:
            if consumir:
                creditos.pop(chave, None)
            return {"permitido": True, "precisa_perguntar": False, "chave": chave,
                    "motivo": f"'{chave}' foi autorizado para esta vez"}
    return {"permitido": False, "precisa_perguntar": True, "chave": chave,
            "motivo": f"'{chave}' altera alguma coisa e ainda não foi autorizado"}


def limpar_sessao(session_id: str) -> None:
    with _TRAVA:
        _DA_SESSAO.pop(session_id or "global", None)
        _UMA_VEZ.pop(session_id or "global", None)


__all__ = ["RESPOSTAS", "assinatura", "avaliar", "limpar_sessao", "responder"]
