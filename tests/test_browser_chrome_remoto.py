"""O servidor MCP do Tato usa as abas do Chrome pela API local.

- a API só aceita com o segredo local, e só os comandos que o Tato pode pedir;
- a conversa do Tato ganha o prefixo `tato-`: nunca alcança a aba de outra conversa;
- navegar para endereço interno é recusado também deste lado;
- o provedor do Tato repassa a ação e o nome de quem está usando.
"""
import pytest

from tato.navegador.browser_chrome_bridge import PonteChrome, segredo_do_tato
from tato.navegador.browser_chrome_provider import ChromeTabSession
from tato.navegador.browser_chrome_remoto import ChromeRemotoProvider
from tato.navegador.browser_contracts import Action


@pytest.fixture(autouse=True)
def _casa(tmp_path, monkeypatch):
    monkeypatch.setenv("TATO_HOME", str(tmp_path))


class PonteLigada(PonteChrome):
    def __init__(self):
        super().__init__()
        self.chamadas = []

    @property
    def conectada(self):
        return True

    async def chamar(self, comando, conversa, *, quem=None, **argumentos):
        self.chamadas.append((comando, conversa, quem, argumentos))
        return {"fonte": "chrome_tab", "dados": "ok"}


def test_o_segredo_fica_no_disco_e_volta_igual():
    assert segredo_do_tato() == segredo_do_tato() and len(segredo_do_tato()) >= 32


class TestPeloTato:
    async def test_prefixo_e_quem_usa(self):
        ponte = PonteLigada()
        r = await ponte.pelo_tato({"comando": "show", "conversa": "abc", "agente": "Claude Code"})
        assert r["ok"] and ponte.chamadas == [("show", "tato-abc", "Claude Code", {})]

    async def test_comando_de_fora_e_recusado(self):
        ponte = PonteLigada()
        r = await ponte.pelo_tato({"comando": "reset", "conversa": "abc"})
        assert not r["ok"] and ponte.chamadas == []

    async def test_endereco_interno_e_recusado(self):
        ponte = PonteLigada()
        acao = {"acao": "navegar", "argumentos": {"url": "http://169.254.169.254/"}}
        r = await ponte.pelo_tato({"comando": "execute", "conversa": "abc", "argumentos": {"action": acao}})
        assert not r["ok"] and ponte.chamadas == []

    async def test_sem_extensao_diz_isso(self):
        r = await PonteChrome().pelo_tato({"comando": "show", "conversa": "abc"})
        assert not r["ok"] and "não conectada" in r["erro"]


def test_a_rota_pede_o_segredo():
    """Outro Tato manda comandos pela ponte aberta só com o segredo local."""
    from starlette.testclient import TestClient

    from tato.navegador.ponte_local import aplicativo

    cliente = TestClient(aplicativo())
    pedido = {"comando": "show", "conversa": "abc"}
    assert cliente.post("/tato/comando", json=pedido).status_code == 401
    assert cliente.post("/tato/comando", json=pedido, headers={"X-Tato-Segredo": "errado"}).status_code == 401
    certo = cliente.post("/tato/comando", json=pedido, headers={"X-Tato-Segredo": segredo_do_tato()})
    assert certo.status_code == 200 and certo.json()["ok"] is False  # sem extensão neste teste


async def test_o_provedor_do_tato_repassa_a_acao():
    chamadas = []

    class Remota:
        def disponivel(self):
            return True

        async def chamar(self, comando, conversa, **argumentos):
            chamadas.append((comando, conversa, argumentos))
            return {"fonte": "chrome_tab", "dados": "pagina", "epoch": 3}

    sessao = ChromeRemotoProvider(Remota()).create_session("conversa-x")
    assert isinstance(sessao, ChromeTabSession) and sessao.esta_viva()
    obs = await sessao.execute(Action(tipo="snapshot", argumentos={}))
    assert obs.dados == "pagina" and obs.epoch == 3
    assert chamadas[0][0] == "execute" and chamadas[0][1] == "conversa-x"


async def test_aba_fechada_pelo_tato_abre_outra_e_segue():
    """Pelo MCP não há cartão para reabrir: a ponte reabre sozinha, só na conversa do Tato."""
    class PonteComAbaFechada(PonteLigada):
        def __init__(self):
            super().__init__()
            self.fechada = True

        async def chamar(self, comando, conversa, *, quem=None, **argumentos):
            self.chamadas.append((comando, conversa))
            if comando == "reset":
                self.fechada = False
                return {"reopened": True}
            if self.fechada:
                raise RuntimeError("A aba do agente foi fechada. Reabra pelo cartão da conversa antes de continuar.")
            return {"fonte": "chrome_tab", "dados": "ok"}

    ponte = PonteComAbaFechada()
    r = await ponte.pelo_tato({"comando": "execute", "conversa": "mcp", "argumentos": {"action": {"acao": "snapshot"}}})
    assert r["ok"] and [c for c, _ in ponte.chamadas] == ["execute", "reset", "execute"]
    assert all(conversa == "tato-mcp" for _, conversa in ponte.chamadas)
