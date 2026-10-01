"""Busca e lê páginas públicas com limites de acesso e tamanho.

Aceita apenas HTTP e HTTPS e recusa endereços privados por padrão.
Valida cada redirecionamento antes da requisição seguinte.
Impõe limites de tempo e bytes às respostas.
A busca informa quando falta uma chave de provedor.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)

TIMEOUT_PADRAO = 20.0
MAX_BYTES = 2_000_000
MAX_TEXTO = 40_000
MAX_REDIRECIONAMENTOS = 4
MAX_RESULTADOS_BUSCA = 20
MAX_PAGINAS_PESQUISA = 10
MAX_TEXTO_POR_FONTE = 12_000
MAX_LEITURAS_SIMULTANEAS = 4
# Site que recusa requisicao sem identificacao devolve 403 e a pesquisa perde
# a fonte. A Wikimedia, por exemplo, exige um contato no proprio User-Agent.
AGENTE = "Tato/1.0 (+https://github.com/Ryanleoncoder/tato-runtime; leitura de pagina publica)"
CABECALHOS = {
    "User-Agent": AGENTE,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

_TAG_RUIDO = re.compile(r"<(script|style|noscript|svg|template)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_ESPACO = re.compile(r"[ \t\r\f\v]+")
_LINHAS = re.compile(r"\n{3,}")


class UrlRecusada(Exception):
    """A URL nao passou nas guardas."""


def _enderecos(host: str) -> List[str]:
    try:
        info = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UrlRecusada(f"host '{host}' nao resolve: {exc}") from exc
    return [item[4][0] for item in info]


def _origens_liberadas() -> frozenset:
    """Lê do ambiente as origens locais autorizadas pelo operador.

    A lista separada por vírgulas não pode ser alterada pelo modelo.
    """
    import os

    bruto = os.environ.get("TATO_ORIGENS_LOCAIS_LIBERADAS", "")
    return frozenset(o.strip().rstrip("/").lower() for o in bruto.split(",") if o.strip())


def validar_url(url: str) -> str:
    """Devolve a URL quando ela e publica e falavel; levanta se nao."""
    bruto = str(url or "").strip()
    if not bruto:
        raise UrlRecusada("url vazia")
    partes = urlparse(bruto)
    if partes.scheme not in ("http", "https"):
        raise UrlRecusada(f"esquema '{partes.scheme or 'vazio'}' nao e permitido; use http ou https")
    if not partes.hostname:
        raise UrlRecusada("url sem host")
    if f"{partes.scheme}://{partes.netloc}".lower() in _origens_liberadas():
        return bruto

    for endereco in _enderecos(partes.hostname):
        ip = ipaddress.ip_address(endereco)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise UrlRecusada(
                f"'{partes.hostname}' aponta para endereco interno ({endereco}); leitura recusada"
            )
    return bruto


def extrair_texto(html: str) -> Dict[str, Any]:
    """HTML para texto legivel. Usa lxml quando existe; regex e o plano B."""
    titulo = ""
    match = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.DOTALL | re.IGNORECASE)
    if match:
        titulo = _ESPACO.sub(" ", _TAGS.sub("", match.group(1))).strip()

    try:
        from lxml import html as lxml_html

        arvore = lxml_html.fromstring(html)
        for elemento in arvore.xpath("//script|//style|//noscript|//nav|//footer|//header"):
            elemento.getparent().remove(elemento)
        texto = arvore.text_content()
    except Exception:
        limpo = _TAG_RUIDO.sub(" ", html or "")
        texto = _TAGS.sub(" ", limpo)

    texto = _ESPACO.sub(" ", texto)
    texto = _LINHAS.sub("\n\n", texto)
    linhas = [linha.strip() for linha in texto.splitlines()]
    texto = "\n".join(linha for linha in linhas if linha)
    return {"titulo": titulo, "texto": texto[:MAX_TEXTO], "truncado": len(texto) > MAX_TEXTO}


async def ler_pagina(url: str, timeout: float = TIMEOUT_PADRAO) -> Dict[str, Any]:
    """Le uma pagina publica e devolve o texto. Nunca levanta."""
    import httpx

    inicio = time.time()
    try:
        alvo = validar_url(url)
    except UrlRecusada as exc:
        return {"ok": False, "url": url, "motivo": str(exc)}

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers=CABECALHOS,
        ) as cliente:
            for _ in range(MAX_REDIRECIONAMENTOS + 1):
                resposta = await cliente.get(alvo)
                if resposta.status_code in (301, 302, 303, 307, 308):
                    destino = resposta.headers.get("location")
                    if not destino:
                        break
                    # Cada salto passa pela mesma guarda; validar so a primeira
                    # URL deixaria o redirecionamento levar para a rede interna.
                    alvo = validar_url(str(httpx.URL(alvo).join(destino)))
                    continue
                break
            else:
                return {"ok": False, "url": url, "motivo": "redirecionamentos demais"}

            resposta.raise_for_status()
            tipo = resposta.headers.get("content-type", "")
            if "html" not in tipo and "text" not in tipo and "json" not in tipo:
                return {"ok": False, "url": alvo, "motivo": f"conteudo '{tipo or 'desconhecido'}' nao e texto"}
            bruto = resposta.text[:MAX_BYTES]
    except UrlRecusada as exc:
        return {"ok": False, "url": url, "motivo": str(exc)}
    except Exception as exc:
        return {"ok": False, "url": url, "motivo": f"{type(exc).__name__}: {exc}"[:200]}

    extraido = extrair_texto(bruto)
    from .destilador import em_linhas, links_numerados

    # Mantém os destinos dos links para permitir abrir as páginas encontradas.
    links = links_numerados(bruto, alvo)
    return {
        "ok": True,
        "url": alvo,
        "titulo": extraido["titulo"],
        "texto": extraido["texto"],
        "links": em_linhas(links),
        "truncado": extraido["truncado"],
        "ms": int((time.time() - inicio) * 1000),
    }


async def ler_bruto(url: str, timeout: float = TIMEOUT_PADRAO) -> Dict[str, Any]:
    """Lê uma resposta pública sem interpretar o corpo.

    Metadados de descoberta não são páginas: ``robots.txt`` precisa manter as
    linhas, ``Link`` mora no cabeçalho e alguns ``agents.json`` chegam como
    octet-stream. Por isso este degrau aplica as mesmas guardas de rede de
    :func:`ler_pagina`, mas não filtra content-type nem extrai HTML.
    """
    import httpx

    inicio = time.time()
    try:
        alvo = validar_url(url)
    except UrlRecusada as exc:
        return {
            "ok": False, "url": url, "status": None, "headers": {},
            "corpo": "", "motivo": str(exc),
        }

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers=CABECALHOS,
        ) as cliente:
            for _ in range(MAX_REDIRECIONAMENTOS + 1):
                async with cliente.stream("GET", alvo) as resposta:
                    if resposta.status_code in (301, 302, 303, 307, 308):
                        destino = resposta.headers.get("location")
                        if not destino:
                            break
                        # A URL de destino é validada antes de qualquer pedido.
                        # Foi justamente o salto público -> rede privada que
                        # tornou insuficiente validar só a primeira URL.
                        alvo = validar_url(str(httpx.URL(alvo).join(destino)))
                        continue

                    bruto = bytearray()
                    truncado = False
                    async for trecho in resposta.aiter_bytes():
                        restante = MAX_BYTES - len(bruto)
                        if restante <= 0:
                            truncado = True
                            break
                        bruto.extend(trecho[:restante])
                        if len(trecho) > restante:
                            truncado = True
                            break

                    codificacao = resposta.encoding or "utf-8"
                    try:
                        corpo = bytes(bruto).decode(codificacao, errors="replace")
                    except LookupError:
                        codificacao = "utf-8"
                        corpo = bytes(bruto).decode(codificacao, errors="replace")
                    status = int(resposta.status_code)
                    headers = dict(resposta.headers)
                    return {
                        "ok": 200 <= status < 300,
                        "url": alvo,
                        "status": status,
                        "headers": headers,
                        "corpo": corpo,
                        "bytes": len(bruto),
                        "truncado": truncado,
                        "encoding": codificacao,
                        "ms": int((time.time() - inicio) * 1000),
                    }
            else:
                return {
                    "ok": False, "url": url, "status": None, "headers": {},
                    "corpo": "", "motivo": "redirecionamentos demais",
                }
    except UrlRecusada as exc:
        return {
            "ok": False, "url": alvo, "status": None, "headers": {},
            "corpo": "", "motivo": str(exc),
        }
    except Exception as exc:
        return {
            "ok": False, "url": alvo, "status": None, "headers": {},
            "corpo": "", "motivo": f"{type(exc).__name__}: {exc}"[:200],
        }

    return {
        "ok": False, "url": alvo, "status": None, "headers": {},
        "corpo": "", "motivo": "redirecionamento sem destino",
    }


def provedor_de_busca(settings: Any) -> Optional[Dict[str, str]]:
    """Qual provedor de busca esta configurado, se algum.

    Guarda o NOME da variavel, como o resto do projeto: o valor fica no
    ambiente e nunca entra em resposta nenhuma.
    """
    candidatos = (
        ("tavily", "TAVILY_API_KEY", "https://api.tavily.com/search"),
        ("brave", "BRAVE_API_KEY", "https://api.search.brave.com/res/v1/web/search"),
        ("serper", "SERPER_API_KEY", "https://google.serper.dev/search"),
    )
    for nome, variavel, endpoint in candidatos:
        if str(getattr(settings, variavel, "") or "").strip():
            return {"nome": nome, "variavel": variavel, "endpoint": endpoint}
    # Sem chave nenhuma a busca simplesmente nunca funcionava, e o modelo
    # respondia de memoria. O DuckDuckGo em HTML nao exige credencial: e pior
    # que uma API paga, e melhor do que nao ter busca.
    return {"nome": "duckduckgo", "variavel": "", "endpoint": "https://html.duckduckgo.com/html/"}


async def buscar(consulta: str, settings: Any, limite: int = 5) -> Dict[str, Any]:
    """Busca na web pelo provedor configurado. Sem chave, diz que nao ha."""
    import httpx

    termo = str(consulta or "").strip()
    limite = max(1, min(int(limite or 5), MAX_RESULTADOS_BUSCA))
    if not termo:
        return {"ok": False, "motivo": "consulta vazia", "resultados": []}

    provedor = provedor_de_busca(settings)
    if provedor is None:
        return {
            "ok": False,
            "motivo": "nenhum provedor de busca configurado (TAVILY_API_KEY, BRAVE_API_KEY ou SERPER_API_KEY)",
            "resultados": [],
        }

    if provedor["nome"] == "duckduckgo":
        return await _buscar_duckduckgo(termo, limite)

    chave = str(getattr(settings, provedor["variavel"], "") or "").strip()
    inicio = time.time()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_PADRAO) as cliente:
            if provedor["nome"] == "tavily":
                resposta = await cliente.post(provedor["endpoint"], json={
                    "api_key": chave, "query": termo, "max_results": limite,
                })
            elif provedor["nome"] == "brave":
                resposta = await cliente.get(provedor["endpoint"], params={"q": termo, "count": limite},
                                             headers={"X-Subscription-Token": chave, "Accept": "application/json"})
            else:
                resposta = await cliente.post(provedor["endpoint"], json={"q": termo, "num": limite},
                                              headers={"X-API-KEY": chave, "Content-Type": "application/json"})
            resposta.raise_for_status()
            dados = resposta.json()
    except Exception as exc:
        return {"ok": False, "motivo": f"{type(exc).__name__}: {exc}"[:200], "resultados": []}

    return {
        "ok": True,
        "provedor": provedor["nome"],
        "consulta": termo,
        "ms": int((time.time() - inicio) * 1000),
        "resultados": _normalizar_resultados(provedor["nome"], dados)[:limite],
    }


_DDG_RESULTADO = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<titulo>.*?)</a>'
    r'(?:.*?class="[^"]*result__snippet[^"]*"[^>]*>(?P<trecho>.*?)</a>)?',
    re.DOTALL | re.IGNORECASE,
)


async def _buscar_duckduckgo(termo: str, limite: int) -> Dict[str, Any]:
    """Busca sem credencial. HTML muda sem aviso, entao falha vira ausencia de
    resultado com motivo, nunca resultado inventado."""
    import httpx
    from urllib.parse import unquote

    inicio = time.time()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_PADRAO, follow_redirects=True) as cliente:
            resposta = await cliente.post(
                "https://html.duckduckgo.com/html/",
                data={"q": termo},
                # O endpoint HTML do DuckDuckGo responde outro formato quando o
                # Accept e amplo: aqui ele precisa ser estrito.
                headers={"User-Agent": AGENTE, "Accept": "text/html"},
            )
            resposta.raise_for_status()
            html = resposta.text
    except Exception as exc:
        return {"ok": False, "motivo": f"{type(exc).__name__}: {exc}"[:200], "resultados": []}

    resultados: List[Dict[str, str]] = []
    for achado in _DDG_RESULTADO.finditer(html):
        url = achado.group("url") or ""
        # O DuckDuckGo embrulha o destino num redirecionador proprio.
        if "uddg=" in url:
            url = unquote(url.split("uddg=", 1)[1].split("&", 1)[0])
        titulo = _TAGS.sub("", achado.group("titulo") or "").strip()
        trecho = _TAGS.sub("", achado.group("trecho") or "").strip()
        if url.startswith("http") and titulo:
            resultados.append({"titulo": titulo[:200], "url": url, "trecho": trecho[:500]})
        if len(resultados) >= limite:
            break

    return {
        "ok": bool(resultados),
        "provedor": "duckduckgo",
        "consulta": termo,
        "ms": int((time.time() - inicio) * 1000),
        "motivo": "" if resultados else "a busca nao devolveu resultado legivel",
        "resultados": resultados,
    }


def _normalizar_resultados(provedor: str, dados: Any) -> List[Dict[str, str]]:
    """Cada provedor tem um formato; a tool devolve sempre o mesmo."""
    if not isinstance(dados, dict):
        return []
    if provedor == "tavily":
        brutos = dados.get("results") or []
        chaves = ("title", "url", "content")
    elif provedor == "brave":
        brutos = ((dados.get("web") or {}).get("results")) or []
        chaves = ("title", "url", "description")
    else:
        brutos = dados.get("organic") or []
        chaves = ("title", "link", "snippet")

    saida = []
    for item in brutos:
        if not isinstance(item, dict):
            continue
        saida.append({
            "titulo": str(item.get(chaves[0]) or "")[:200],
            "url": str(item.get(chaves[1]) or ""),
            "trecho": str(item.get(chaves[2]) or "")[:500],
        })
    return saida


async def pesquisar(
    consulta: str,
    settings: Any,
    *,
    paginas: int = 3,
    limite: int = 5,
) -> Dict[str, Any]:
    """Busca resultados e lê as páginas selecionadas.

    Retorna a URL de cada página para permitir a atribuição da fonte.
    """
    limite = max(1, min(int(limite or 5), MAX_RESULTADOS_BUSCA))
    paginas = max(0, min(int(paginas or 0), MAX_PAGINAS_PESQUISA, limite))
    encontrados = await buscar(consulta, settings, limite=limite)
    if not encontrados.get("ok"):
        return {**encontrados, "paginas": []}

    resultados: List[Dict[str, Any]] = []
    urls_vistas: set[str] = set()
    for item in encontrados.get("resultados") or []:
        url = str(item.get("url") or "")
        chave = _chave_url(url)
        if not chave or chave in urls_vistas:
            continue
        urls_vistas.add(chave)
        resultados.append(item)

    semaforo = asyncio.Semaphore(MAX_LEITURAS_SIMULTANEAS)

    async def _ler(item: Dict[str, Any]) -> Dict[str, Any]:
        url = str(item.get("url") or "")
        async with semaforo:
            pagina = await ler_pagina(url)
        if not pagina.get("ok"):
            return {"url": url, "titulo": item.get("titulo", ""),
                    "ok": False, "motivo": pagina.get("motivo", "")}
        texto = str(pagina.get("texto") or "")
        return {
            "url": url,
            "titulo": pagina.get("titulo") or item.get("titulo", ""),
            "ok": True,
            "texto": texto[:MAX_TEXTO_POR_FONTE],
            "truncado": bool(pagina.get("truncado")) or len(texto) > MAX_TEXTO_POR_FONTE,
        }

    lidas = await asyncio.gather(*(_ler(item) for item in resultados[:paginas]))

    return {
        **encontrados,
        "resultados": resultados,
        "paginas": lidas,
        "lidas": sum(1 for p in lidas if p.get("ok")),
        "falharam": [p["url"] for p in lidas if not p.get("ok")],
        "fontes_unicas": len(resultados),
    }


_PARAMETROS_DE_RASTREIO = {"fbclid", "gclid", "mc_cid", "mc_eid"}


def _chave_url(url: str) -> str:
    """Normaliza o suficiente para nao ler a mesma fonte duas vezes."""
    try:
        partes = urlparse(str(url or "").strip())
    except Exception:
        return ""
    if partes.scheme not in ("http", "https") or not partes.netloc:
        return ""
    query = urlencode(sorted(
        (chave, valor)
        for chave, valor in parse_qsl(partes.query, keep_blank_values=True)
        if not chave.lower().startswith("utm_") and chave.lower() not in _PARAMETROS_DE_RASTREIO
    ))
    caminho = partes.path.rstrip("/") or "/"
    return urlunparse((partes.scheme.lower(), partes.netloc.lower(), caminho, "", query, ""))
