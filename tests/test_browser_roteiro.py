"""O roteiro: vários passos numa chamada, parando onde o agente decide.

Roda no provedor falso (fixture de vagas): o botão `e1` revela a vaga
"Engenheira Python". Nenhum navegador de verdade é aberto.
"""
from pathlib import Path

import pytest

from tato.politica import portao
from tato.estado import EstadoDoTurno
from tato.navegador import browser_registry
from tato import permissoes
from tato.navegador.browser_contracts import Action
from tato.navegador.browser_fake_provider import FakeBrowserProvider

FIXTURE = Path(__file__).parent / "fixtures" / "browser" / "vagas.html"


@pytest.fixture
def provedor():
    browser_registry._limpar_para_testes()
    falso = FakeBrowserProvider(FIXTURE)
    browser_registry.registrar(falso)
    yield falso
    browser_registry._limpar_para_testes()


def _roteiro(*passos):
    return Action("roteiro", {"passos": list(passos)})


async def _rodar(action, nome="roteiro"):
    return await browser_registry.despachar(action, EstadoDoTurno.new(nome, "u"))


async def test_roteiro_inteiro_numa_chamada(provedor):
    r = await _rodar(_roteiro(
        {"acao": "clicar", "argumentos": {"ref": "e1"}, "consequencia": "nenhuma"},
        {"acao": "conferir", "argumentos": {"texto": "Engenheira Python"}},
    ))
    assert r["ok"] and r["concluido"]
    assert [f["acao"] for f in r["feitos"]] == ["clicar", "conferir"]


async def test_conferir_que_quebra_para_ali_e_devolve_a_pagina(provedor):
    r = await _rodar(_roteiro(
        {"acao": "conferir", "argumentos": {"texto": "Engenheira Python"}},
        {"acao": "clicar", "argumentos": {"ref": "e1"}, "consequencia": "nenhuma"},
    ))
    assert r["ok"] is False and r["parou_no_passo"] == 1
    assert "conferir falhou" in r["motivo"]
    assert r["restantes"] == [{"acao": "clicar", "argumentos": {"ref": "e1"}, "consequencia": "nenhuma"}]
    assert r["observation"]["dados"]["arvore"], "devolveu a pagina para o agente decidir"


async def test_conferir_ignora_acento_e_caixa(provedor):
    r = await _rodar(_roteiro({"acao": "conferir", "argumentos": {"texto": "resultados ATUALIZADOS"}}))
    assert r["concluido"]


async def test_pensar_devolve_o_controle_com_o_que_falta(provedor):
    r = await _rodar(_roteiro(
        {"acao": "clicar", "argumentos": {"ref": "e1"}, "consequencia": "nenhuma"},
        {"acao": "pensar"},
        {"acao": "clicar", "argumentos": {"ref": "e2"}, "consequencia": "nenhuma"},
    ))
    assert r["ok"] and not r["concluido"] and r["parou_no_passo"] == 2
    assert len(r["restantes"]) == 1


async def test_esperar_que_nao_chega_para_com_motivo(provedor):
    r = await _rodar(_roteiro({"acao": "esperar", "argumentos": {"texto": "nunca aparece", "ms": 300}}))
    assert r["ok"] is False and "não apareceu" in r["motivo"]


async def test_passo_com_consequencia_pergunta_uma_vez_pelo_roteiro_inteiro(provedor):
    r = await _rodar(_roteiro(
        {"acao": "clicar", "argumentos": {"ref": "e1"}, "consequencia": "nenhuma"},
        {"acao": "clicar", "argumentos": {"ref": "candidatar"}, "consequencia": "comunicar"},
    ))
    assert r["precisa_permissao"] is True and r["so_desta_vez"] is True
    assert r["chave"].startswith("browser_roteiro#")
    assert "candidatar" in r["comando"]


async def test_a_aprovacao_vale_so_para_aquele_roteiro(provedor):
    state = EstadoDoTurno.new("aprovacao", "u")
    um = _roteiro({"acao": "clicar", "argumentos": {"ref": "e1"}})
    outro = _roteiro({"acao": "clicar", "argumentos": {"ref": "e2"}})
    assert um.chave_de_permissao != outro.chave_de_permissao
    permissoes.responder(um.chave_de_permissao, "sessao", state.session_id)
    assert (await browser_registry.despachar(um, state))["ok"]
    assert (await browser_registry.despachar(outro, state)).get("precisa_permissao")


def test_navegar_interno_dentro_do_roteiro_e_recusado():
    decisao = portao.evaluate(EstadoDoTurno.new("x", "u"), "browser", "vai", {
        "acao": "roteiro",
        "argumentos": {"passos": [{"acao": "navegar", "argumentos": {"url": "http://169.254.169.254/"}}]},
    })
    assert decisao.allowed is False and "navegar recusado" in decisao.reason


def test_passo_inventado_e_recusado():
    decisao = portao.evaluate(EstadoDoTurno.new("x", "u"), "browser", "vai", {
        "acao": "roteiro", "argumentos": {"passos": [{"acao": "teleportar"}]},
    })
    assert decisao.allowed is False and "teleportar" in decisao.reason


def test_nomes_da_ferramenta_valem_como_passo_do_roteiro():
    """`imagens` e `snapshot` são ações da própria ferramenta; dentro do roteiro
    eram recusados e o modelo desistia do fluxo."""
    from tato.navegador.browser_contracts import Action, tipo_do_passo

    roteiro = Action("roteiro", {"passos": [{"acao": "clicar", "argumentos": {"ref": "e1"}},
                                            {"acao": "imagens"}, {"acao": "snapshot"}]})
    assert [p.tipo for p in roteiro.passos()] == ["clicar"]
    assert [tipo_do_passo(p) for p in roteiro.argumentos["passos"]] == ["clicar", "ver", "pensar"]


async def test_passo_que_quebra_diz_qual_e_o_que_ja_rodou():
    """O erro do navegador num passo vira parada com o número dele, não um erro solto."""
    from tato.navegador.browser_contracts import Action, Observation
    from tato.navegador.browser_roteiro import executar

    class Aba:
        def __init__(self):
            self.vezes = 0

        async def execute(self, action):
            self.vezes += 1
            if action.tipo == "clicar":
                raise RuntimeError("clicar precisa de `ref` (da última leitura) ou `nome` (o texto do elemento)")
            return Observation(fonte="teste", dados="Example Domain", epoch=self.vezes)

    roteiro = Action("roteiro", {"passos": [
        {"acao": "navegar", "argumentos": {"url": "https://example.org/"}},
        {"acao": "clicar", "argumentos": {}, "consequencia": "nenhuma"},
        {"acao": "pensar"}]})
    r = await executar(Aba(), roteiro)
    assert not r["ok"] and r["parou_no_passo"] == 2 and "passo 2 (clicar) falhou" in r["motivo"]
    assert [f["acao"] for f in r["feitos"]] == ["navegar"] and len(r["restantes"]) == 1
