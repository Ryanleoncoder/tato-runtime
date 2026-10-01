from pathlib import Path

import pytest

from tato.estado import EstadoDoTurno
from tato.navegador import browser_registry
from tato.navegador.browser_contracts import Action
from tato.navegador.browser_fake_provider import FakeBrowserProvider


FIXTURE = Path("tests/fixtures/browser/vagas.html")


@pytest.fixture(autouse=True)
def _registro_limpo():
    browser_registry._limpar_para_testes()
    yield
    browser_registry._limpar_para_testes()


@pytest.mark.asyncio
async def test_snapshot_clique_snapshot_preserva_a_mesma_sessao(monkeypatch):
    provider = FakeBrowserProvider(FIXTURE)
    criacoes = 0
    criar_original = provider.create_session

    def contar(task_id):
        nonlocal criacoes
        criacoes += 1
        return criar_original(task_id)

    monkeypatch.setattr(provider, "create_session", contar)
    browser_registry.registrar(provider)
    state = EstadoDoTurno.new("case-vagas", "u")
    monkeypatch.setattr(
        "tato.politica.avaliar",
        lambda *_a, **_k: {"permitido": True, "precisa_perguntar": False, "motivo": "teste"},
    )

    antes = await browser_registry.despachar(Action("snapshot"), state, provider_name="fixture")
    arvore_antes = antes["observation"]["dados"]["arvore"]
    assert not any(item["name"] == "Engenheira Python" for item in arvore_antes)

    clique = await browser_registry.despachar(
        Action("clicar", {"ref": "e1"}), state, provider_name="fixture",
    )
    depois = await browser_registry.despachar(Action("snapshot"), state, provider_name="fixture")

    assert clique["observation"]["epoch"] == 1
    assert depois["observation"]["epoch"] == 1
    assert any(
        item["role"] == "heading" and item["name"] == "Engenheira Python"
        for item in depois["observation"]["dados"]["arvore"]
    )
    assert criacoes == 1
    assert browser_registry.encerrar(state.session_id, "fixture") == 1
    assert browser_registry.encerrar(state.session_id, "fixture") == 0


@pytest.mark.asyncio
async def test_sessoes_sao_isoladas_por_tarefa(monkeypatch):
    provider = FakeBrowserProvider(FIXTURE)
    browser_registry.registrar(provider)
    monkeypatch.setattr(
        "tato.politica.avaliar",
        lambda *_a, **_k: {"permitido": True, "precisa_perguntar": False, "motivo": "teste"},
    )
    a = EstadoDoTurno.new("sessao-a", "u")
    b = EstadoDoTurno.new("sessao-b", "u")

    await browser_registry.despachar(Action("clicar", {"ref": "e1"}), a, provider_name="fixture")
    snap_b = await browser_registry.despachar(Action("snapshot"), b, provider_name="fixture")

    assert snap_b["observation"]["epoch"] == 0
    assert not any(
        item["name"] == "Engenheira Python"
        for item in snap_b["observation"]["dados"]["arvore"]
    )
