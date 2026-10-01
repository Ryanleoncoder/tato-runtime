"""A lista numerada de links da página, sem navegador (degrau 1 da escada).

O `ler_pagina` devolvia só o texto, e os links se perdiam. Num catálogo de
vagas o texto diz "Engenheira Python · Remoto", mas o caminho para a vaga
sumia, e o único jeito de seguir era abrir o navegador. A lista numerada é a
ideia do `oc` (`[1] Show HN: ...`), feita aqui: o agente lê o catálogo em
poucas centenas de tokens e segue o link pela URL.

Só lê: não clica nem preenche. Para agir, o degrau é o `browser`.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import urljoin, urlparse

MAX_LINKS = 60
MAX_TEXTO_DO_LINK = 90

_ESPACO = re.compile(r"\s+")
_A_REGEX = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")
# Texto de link que nao diz nada sozinho: fica de fora da lista.
_VAZIOS = {"", "#", "aqui", "clique aqui", "saiba mais", "leia mais", "ver mais", "more", "here"}


def _candidatos(html: str) -> List[tuple]:
    try:
        from lxml import html as lxml_html

        arvore = lxml_html.fromstring(html)
        for ruido in arvore.xpath("//script|//style|//noscript"):
            ruido.getparent().remove(ruido)
        return [(a.get("href") or "", a.text_content() or a.get("title") or a.get("aria-label") or "")
                for a in arvore.xpath("//a[@href]")]
    except Exception:
        return [(href, _TAGS.sub(" ", texto)) for href, texto in _A_REGEX.findall(html or "")]


def links_numerados(html: str, base: str) -> List[Dict[str, Any]]:
    """`[{n, texto, url}]` em ordem de aparição, sem repetir URL nem link vazio."""
    vistos = set()
    saida: List[Dict[str, Any]] = []
    for href, texto in _candidatos(html):
        href = (href or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base, href).split("#", 1)[0]
        if urlparse(url).scheme not in ("http", "https") or url in vistos:
            continue
        rotulo = _ESPACO.sub(" ", texto or "").strip()
        if rotulo.lower() in _VAZIOS:
            continue
        vistos.add(url)
        saida.append({"n": len(saida) + 1, "texto": rotulo[:MAX_TEXTO_DO_LINK], "url": url})
        if len(saida) >= MAX_LINKS:
            break
    return saida


def em_linhas(links: List[Dict[str, Any]]) -> str:
    """A forma compacta que vai para o modelo: `[3] Engenheira Python -> url`."""
    return "\n".join(f"[{l['n']}] {l['texto']} -> {l['url']}" for l in links)


__all__ = ["em_linhas", "links_numerados"]
