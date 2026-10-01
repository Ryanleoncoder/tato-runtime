"""Leitura crua para descoberta de metadados de sites."""
import httpx

from tato.navegador import web


def _cliente_com(transporte):
    cliente_real = httpx.AsyncClient

    def criar(**opcoes):
        return cliente_real(transport=transporte, **opcoes)

    return criar


async def test_ler_bruto_preserva_corpo_headers_status_e_tipo_generico(monkeypatch):
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Content-Type": "application/octet-stream",
                "ETag": '"site-v3"',
                "Link": '</llms.txt>; rel="describedby"',
            },
            content=b"User-agent: *\nDisallow: /conta\n",
            request=request,
        )

    monkeypatch.setattr(web, "_enderecos", lambda _host: ["93.184.216.34"])
    monkeypatch.setattr(httpx, "AsyncClient", _cliente_com(httpx.MockTransport(responder)))

    resultado = await web.ler_bruto("https://loja.example/robots.txt")

    assert resultado["ok"] is True
    assert resultado["status"] == 200
    assert resultado["headers"]["etag"] == '"site-v3"'
    assert resultado["headers"]["link"] == '</llms.txt>; rel="describedby"'
    assert resultado["corpo"] == "User-agent: *\nDisallow: /conta\n"


async def test_ler_bruto_recusa_redirecionamento_para_ip_privado_antes_do_pedido(monkeypatch):
    pedidos = []

    def responder(request: httpx.Request) -> httpx.Response:
        pedidos.append(str(request.url))
        return httpx.Response(
            302,
            headers={"Location": "http://127.0.0.1/segredo"},
            request=request,
        )

    def enderecos(host: str):
        return ["127.0.0.1"] if host == "127.0.0.1" else ["93.184.216.34"]

    monkeypatch.setattr(web, "_enderecos", enderecos)
    monkeypatch.setattr(httpx, "AsyncClient", _cliente_com(httpx.MockTransport(responder)))

    resultado = await web.ler_bruto("https://loja.example/atalho")

    assert resultado["ok"] is False
    assert "interno" in resultado["motivo"]
    assert pedidos == ["https://loja.example/atalho"]
