"""Compara a acessibilidade antes e depois de cada gesto.

Classifica o resultado como confirmado, mudou, sem efeito aparente ou
impossível de confirmar. A classificação orienta a próxima observação
e evita repetir gestos que já tiveram efeito.
"""
from __future__ import annotations

import re

from typing import Any, Dict, List, Optional, Tuple

from .computador_elementos import Elemento

_TETO = 10


def _programa(janela: str) -> str:
    """A parte do título que não muda com o documento: "Sem título - Bloco de
    notas" e "notas.txt - Bloco de notas" são a mesma janela."""
    return re.split(r"\s[-–—]\s", str(janela or ""))[-1].strip()


def _chave(e: Elemento) -> Tuple[str, str, str]:
    return _programa(e.janela), e.papel, e.nome


def o_que_mudou(antes: Optional[List[Elemento]], agora: List[Elemento]) -> Dict[str, Any]:
    """O que apareceu, sumiu e mudou de valor ou estado entre duas leituras."""
    if antes is None:
        return {}
    de_antes = {_chave(e): e for e in antes}
    de_agora = {_chave(e): e for e in agora}
    mudou: Dict[str, Any] = {}
    apareceram = [e.nome for c, e in de_agora.items() if c not in de_antes and e.nome][:_TETO]
    sumiram = [e.nome for c, e in de_antes.items() if c not in de_agora and e.nome][:_TETO]
    trocaram = []
    for c, e in de_agora.items():
        a = de_antes.get(c)
        if a is None:
            continue
        diferenca: Dict[str, Any] = {}
        if a.valor != e.valor:
            diferenca["valor"] = [a.valor, e.valor]
        if a.estado != e.estado:
            diferenca["estado"] = [a.estado, e.estado]
        if a.ativo != e.ativo:
            diferenca["desativado"] = [not a.ativo, not e.ativo]
        if diferenca:
            trocaram.append({"nome": e.nome, **diferenca})
    if apareceram:
        mudou["apareceram"] = apareceram
    if sumiram:
        mudou["sumiram"] = sumiram
    if trocaram:
        mudou["trocaram"] = trocaram[:_TETO]
    return {"mudou": mudou} if mudou else {}


def _o_mesmo(alvo: Optional[Elemento], agora: List[Elemento]) -> Optional[Elemento]:
    if alvo is None:
        return None
    return next((e for e in agora if _chave(e) == _chave(alvo)), None)


def _degrau(acao: str, argumentos: Dict[str, Any]) -> str:
    if acao == "invocar":
        return "tente `clicar` no mesmo elemento"
    if acao == "definir_valor":
        return "clique no campo e use `digitar`"
    if argumentos.get("elemento") is not None:
        return "peça `ver` e confira se o elemento é mesmo o alvo"
    return "peça `ver` com `marcar` e mire por número"


def veredito(acao: str, argumentos: Dict[str, Any], alvo: Optional[Elemento],
             antes: Optional[List[Elemento]], agora: Optional[List[Elemento]]) -> Dict[str, Any]:
    """`alvo` é o elemento em que o gesto mirou (o número, ou o foco no
    `digitar`), como estava antes; `antes` e `agora` são as leituras."""
    if agora is None:
        return {"veredito": "nao_da_para_confirmar",
                "proximo": ("sem leitura da acessibilidade: confira no print se o gesto fez efeito ANTES de "
                            "repetir; se nada mudou, procure o motivo em vez de repetir")}
    depois = _o_mesmo(alvo, agora)
    if depois is not None:
        if acao in ("digitar", "definir_valor"):
            texto = str(argumentos.get("texto") if acao == "digitar" else argumentos.get("valor") or "").strip()
            if texto and (depois.valor == texto or (acao == "digitar" and texto in depois.valor)):
                return {"veredito": "confirmado", "proximo": f"«{depois.nome}» ficou com o valor pedido; não repita"}
        if acao in ("clicar", "invocar") and depois.estado and depois.estado != alvo.estado:
            return {"veredito": "confirmado",
                    "proximo": f"«{depois.nome}» ficou {depois.estado}; não repita"}
    if antes is None:
        return {"veredito": "nao_da_para_confirmar",
                "proximo": ("não havia leitura de antes do gesto para comparar: confira nesta lista se foi o "
                            "que você queria antes de seguir; não repita sem conferir")}
    mudanca = o_que_mudou(antes, agora)
    if mudanca:
        return {"veredito": "mudou", **mudanca,
                "proximo": "a tela mudou: confira em `mudou` se foi o que você queria antes de seguir; não repita"}
    return {"veredito": "sem_efeito_aparente",
            "proximo": (f"a lista de elementos ficou igual. Não repita o mesmo gesto: {_degrau(acao, argumentos)}. "
                        "A lista é da janela em foco: um app que abriu atrás dela, ou que ainda está "
                        "carregando, não aparece aqui. `janelas` mostra o que abriu; `ver` mostra a tela inteira, "
                        "e também o que a lista não descreve (desenho, canvas)")}


__all__ = ["o_que_mudou", "veredito"]
