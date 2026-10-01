import pytest

from tato.politica import portao
from tato.estado import EstadoDoTurno
from tato.navegador import browser_registry
from tato.navegador.browser_contracts import Action
from tato.navegador.browser_provider import BrowserProvider


class _ProvedorIndisponivel(BrowserProvider):
    def __init__(self, name: str):
        self._name = name
        self.consultas = 0
        self.sessoes = 0

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        self.consultas += 1
        return False

    def create_session(self, task_id: str):
        self.sessoes += 1
        raise AssertionError("provedor indisponível não cria sessão")

    def close_session(self, session_id: str) -> bool:
        return True

    def emergency_cleanup(self, session_id: str) -> None:
        return None


@pytest.fixture(autouse=True)
def _registro_limpo():
    browser_registry._limpar_para_testes()
    yield
    browser_registry._limpar_para_testes()


def _state() -> EstadoDoTurno:
    return EstadoDoTurno.new("browser-test", "u1")


def test_argumentos_planos_do_modelo_nao_perdem_url_ou_ref():
    navegar = Action.de_argumentos({"action": "navegar", "url": "https://example.com"})
    clicar = Action.de_argumentos({"action": "clicar", "ref": "e7", "consequencia": "nenhuma"})
    assert navegar.argumentos == {"url": "https://example.com"}
    assert clicar.argumentos == {"ref": "e7"}
    assert clicar.consequencia == "nenhuma"


def test_observacao_de_browser_nao_pede_permissao(monkeypatch):
    def nao_deveria_avaliar(*_args, **_kwargs):
        raise AssertionError("snapshot não passa pela escada de mutação")

    monkeypatch.setattr("tato.permissoes.avaliar", nao_deveria_avaliar)
    decisao = portao.evaluate(_state(), "browser", "veja a página", {"acao": "snapshot"})
    assert decisao.allowed is True
    assert "observa" in decisao.reason


async def test_acao_mutante_sem_autorizacao_pede_permissao_antes_do_provedor(monkeypatch):
    um = _ProvedorIndisponivel("local")
    browser_registry.registrar(um)
    monkeypatch.setattr(
        "tato.permissoes.avaliar",
        lambda *_args, **_kwargs: {
            "permitido": False,
            "precisa_perguntar": True,
            "motivo": "browser_clicar ainda não foi autorizado",
        },
    )

    resultado = await browser_registry.despachar(Action("clicar", {"ref": "e12"}), _state())

    # mesmo formato do shell: e isto que abre o cartao, e nao um bloqueio
    assert resultado["precisa_permissao"] is True
    assert resultado["chave"] == "browser_clicar"
    assert resultado["comando"] == "browser_clicar e12"
    assert resultado["executado"] is False
    assert um.consultas == 0 and um.sessoes == 0


async def test_acao_negada_antes_e_recusa_sem_pergunta(monkeypatch):
    monkeypatch.setattr(
        "tato.permissoes.avaliar",
        lambda *_a, **_k: {"permitido": False, "precisa_perguntar": False, "motivo": "negado antes"},
    )
    resultado = await browser_registry.despachar(Action("clicar", {"ref": "e1"}), _state())
    assert resultado.get("recusado") is True
    assert not resultado.get("precisa_permissao")


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",
    "http://127.0.0.1:8000/",
    "file:///C:/Users/alguem/.ssh/id_rsa",
    "",
])
def test_navegar_para_endereco_interno_e_recusado_mesmo_aprovado(monkeypatch, url):
    monkeypatch.setattr(
        "tato.permissoes.avaliar",
        lambda *_a, **_k: {"permitido": True, "precisa_perguntar": False, "motivo": "permitir tudo"},
    )
    decisao = portao.evaluate(
        _state(), "browser", "abre", {"acao": "navegar", "argumentos": {"url": url}},
    )
    assert decisao.allowed is False
    assert "navegar recusado" in decisao.reason


def test_navegar_para_url_publica_segue_para_autorizacao(monkeypatch):
    monkeypatch.setattr("tato.navegador.web._enderecos", lambda _host: ["93.184.216.34"])
    decisao = portao.evaluate(
        _state(), "browser", "abre", {"acao": "navegar", "argumentos": {"url": "https://exemplo.com/vagas"}},
    )
    assert decisao.allowed is True


async def test_dois_provedores_indisponiveis_terminam_com_motivo_claro():
    um = _ProvedorIndisponivel("servidor")
    dois = _ProvedorIndisponivel("maquina-do-usuario")
    browser_registry.registrar(um)
    browser_registry.registrar(dois)

    resultado = await browser_registry.despachar(Action("snapshot"), _state())

    assert resultado == {
        "ok": False,
        "executado": False,
        "fase": "provider",
        "motivo": "nenhum provedor de navegador disponível",
        "trace": {
            "proposed_tool": "browser",
            "browser_action": "snapshot",
            "harness_allowed": True,
            "harness_reason": "browser.snapshot apenas observa a página",
        },
    }
    assert um.sessoes == 0 and dois.sessoes == 0


@pytest.mark.parametrize("url", [
    "https://exemplo.com/coleta?api_key=sk-ant-abcdefghijklmnop",
    "https://exemplo.com/?t=sk%2Dantabcdefghijklmnopq",
    "https://exemplo.com/?token=ghp_abcdefghijklmnopqrstuvwxyz0123",
])
def test_navegar_com_credencial_na_url_e_recusado(monkeypatch, url):
    monkeypatch.setattr("tato.navegador.web._enderecos", lambda _host: ["93.184.216.34"])
    decisao = portao.evaluate(
        _state(), "browser", "abre", {"acao": "navegar", "argumentos": {"url": url}},
    )
    assert decisao.allowed is False
    assert "credencial" in decisao.reason
