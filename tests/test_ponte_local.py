"""A porta da extensão: o primeiro Tato abre, os seguintes repassam para ele."""
import asyncio
import socket

import pytest

from tato.navegador import browser_registry, ponte_local
from tato.navegador.browser_chrome_provider import ChromeTabProvider
from tato.navegador.browser_chrome_remoto import ChromeRemotoProvider


@pytest.fixture
def porta_livre(monkeypatch, tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]
    monkeypatch.setenv("TATO_PORTA", str(porta))
    monkeypatch.setenv("TATO_HOME", str(tmp_path))
    browser_registry._limpar_para_testes()
    yield porta
    browser_registry._limpar_para_testes()


async def test_com_a_porta_livre_o_tato_abre_a_ponte(porta_livre):
    try:
        assert await ponte_local.preparar() == "ponte"
        assert any(isinstance(p, ChromeTabProvider) and not isinstance(p, ChromeRemotoProvider)
                   for p in browser_registry.listar())
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", porta_livre), timeout=0.2):
                    break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            pytest.fail("a ponte não abriu a porta")
    finally:
        await ponte_local.fechar()


async def test_com_a_porta_ocupada_vira_cliente(porta_livre):
    ocupante = socket.socket()
    ocupante.bind(("127.0.0.1", porta_livre))
    ocupante.listen(1)
    try:
        assert await ponte_local.preparar() == "cliente"
        assert any(isinstance(p, ChromeRemotoProvider) for p in browser_registry.listar())
    finally:
        ocupante.close()
