"""Executa uma sequência de ações de navegador numa chamada.

`conferir` e `esperar` validam condições antes de avançar.
`ver` e `pensar` devolvem o controle ao agente para nova decisão.
Falhas de navegação e bloqueios interrompem a sequência.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import unicodedata
from typing import Any, Callable, Dict, List

from .browser_contracts import ACOES_MUTANTES, Action, Observation, tipo_do_passo

_TETO_ESPERA_MS = 10_000
_INTERVALO_ESPERA_S = 0.25


def _normalizar(texto: str) -> str:
    semacento = unicodedata.normalize("NFD", str(texto or ""))
    return "".join(c for c in semacento if unicodedata.category(c) != "Mn").lower()


def _contem(obs: Observation, texto: str) -> bool:
    corpo = json.dumps(obs.dados, ensure_ascii=False) + " " + json.dumps(dict(obs.metadados), ensure_ascii=False)
    return _normalizar(texto) in _normalizar(corpo)


async def _rodar(capability, action: Action) -> Observation:
    obs = capability.execute(action)
    if inspect.isawaitable(obs):
        obs = await obs
    if not isinstance(obs, Observation):
        raise TypeError("provedor devolveu observação fora do contrato")
    return obs


async def _pagina_inteira(capability) -> Observation:
    return await _rodar(capability, Action("snapshot", {"completo": True}))


def _sem_aviso(indice: int, status: str, leitura: str = "") -> None:
    return None


async def executar(capability, action: Action, avisar: Callable[..., None] = _sem_aviso) -> Dict[str, Any]:
    """Roda o roteiro e diz onde e por que parou, se parou.

    `avisar(indice, status, leitura)` conta cada passo ao cartao do chat:
    "running" antes, "ok"/"error" depois, com "texto" ou "print".
    """
    passos: List[Dict[str, Any]] = list(action.argumentos.get("passos") or [])
    feitos: List[Dict[str, Any]] = []
    ultima: Observation | None = None

    def parar(indice: int, motivo: str, obs: Observation | None, *, ok: bool,
              leitura: str = "texto") -> Dict[str, Any]:
        avisar(indice, "ok" if ok else "error", leitura)
        return {
            "ok": ok,
            "concluido": False,
            "parou_no_passo": indice + 1,
            "motivo": motivo,
            "feitos": feitos,
            "restantes": passos[indice + 1:],
            "observation": obs.para_dict() if obs else None,
        }

    for i, passo in enumerate(passos):
        tipo = tipo_do_passo(passo)
        avisar(i, "running")

        if tipo in ACOES_MUTANTES:
            try:
                ultima = await _rodar(capability, Action.de_argumentos(passo))
            except Exception as exc:
                return parar(i, f"passo {i + 1} ({tipo}) falhou: {exc}", ultima, ok=False)
            feitos.append({"passo": i + 1, "acao": tipo, "epoch": ultima.epoch})
            recusada = ultima.metadados.get("navegacao_recusada")
            bloqueio = ultima.metadados.get("bloqueio")
            if recusada or bloqueio:
                return parar(i, f"a página recusou a navegação: {recusada}" if recusada
                             else f"bloqueio na página: {bloqueio}", ultima, ok=False)
            avisar(i, "ok", "texto")
            continue

        texto = str((passo.get("argumentos") or {}).get("texto") or passo.get("texto") or "")
        if tipo == "esperar":
            teto = min(int((passo.get("argumentos") or {}).get("ms") or _TETO_ESPERA_MS), _TETO_ESPERA_MS)
            limite = time.monotonic() + teto / 1000
            while True:
                ultima = await _pagina_inteira(capability)
                if not texto or _contem(ultima, texto):
                    break
                if time.monotonic() >= limite:
                    return parar(i, f"esperar: '{texto}' não apareceu em {teto} ms", ultima, ok=False)
                await asyncio.sleep(_INTERVALO_ESPERA_S)
            feitos.append({"passo": i + 1, "acao": tipo})
            avisar(i, "ok", "texto")
        elif tipo == "conferir":
            # Pagina inteira, nao o diff: o texto certo pode ja estar ali desde antes.
            ultima = await _pagina_inteira(capability)
            if not _contem(ultima, texto):
                return parar(i, f"conferir falhou: '{texto}' não está na página", ultima, ok=False)
            feitos.append({"passo": i + 1, "acao": tipo})
            avisar(i, "ok", "texto")
        elif tipo == "ver":
            ultima = await _rodar(capability, Action("imagens", passo.get("argumentos") or {}))
            return parar(i, "ver: o print está na observação", ultima, ok=True, leitura="print")
        elif tipo == "pensar":
            return parar(i, "pensar: decida o resto com esta página", ultima or await _pagina_inteira(capability), ok=True)

    return {
        "ok": True,
        "concluido": True,
        "feitos": feitos,
        "observation": ultima.para_dict() if ultima else None,
    }


__all__ = ["executar"]
