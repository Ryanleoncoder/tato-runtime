"""Confirmação por consequência: o que pergunta é o efeito, não o verbo.

A cena do case da vaga é o agente achar o botão "Candidatar", parar e
perguntar. Com permissão por verbo e "permitir tudo" ligado (o estado desta
máquina), o clique passava direto. E perguntar a cada clique em filtro ou
paginação ensinava a pessoa a aprovar no automático.
"""
import pytest

from tato.politica import portao
from tato.estado import EstadoDoTurno
from tato.navegador import browser_registry
from tato import permissoes
from tato.navegador.browser_contracts import Action, Observation
from tato.navegador.browser_provider import BrowserProvider


class _Sessao:
    session_id = "s"

    def __init__(self, dono):
        self._dono = dono

    async def execute(self, action):
        self._dono.acoes.append(action.tipo)
        return Observation(fonte="falso", dados={})


class _Provedor(BrowserProvider):
    def __init__(self):
        self.acoes = []

    @property
    def name(self):
        return "falso"

    def is_available(self):
        return True

    def create_session(self, task_id):
        return _Sessao(self)

    def close_session(self, session_id):
        return True

    def emergency_cleanup(self, session_id):
        return None


@pytest.fixture
def provedor():
    browser_registry._limpar_para_testes()
    falso = _Provedor()
    browser_registry.registrar(falso)
    yield falso
    browser_registry._limpar_para_testes()


def _state(nome="consequencia"):
    return EstadoDoTurno.new(nome, "u")


async def _despachar(acao, argumentos, consequencia="", state=None):
    return await browser_registry.despachar(Action(acao, argumentos, consequencia), state or _state())


async def test_clique_sem_consequencia_nao_pergunta(provedor):
    resultado = await _despachar("clicar", {"ref": "filtro-remoto"}, "nenhuma")
    assert resultado["ok"] is True and provedor.acoes == ["clicar"]


async def test_clique_sem_declarar_pergunta(provedor):
    resultado = await _despachar("clicar", {"ref": "e4"})
    assert resultado["precisa_permissao"] is True
    assert resultado["chave"] == "browser_clicar"


async def test_a_sessao_aprovada_nao_libera_a_proxima_candidatura(provedor):
    """Candidatura e envio se confirmam na hora: aprovar a sessão não vale para eles."""
    state = _state("sessao")
    permissoes.responder("browser_comunicar", "sessao", state.session_id)
    try:
        resultado = await _despachar("clicar", {"ref": "candidatar"}, "comunicar", state)
        assert resultado.get("precisa_permissao") is True
    finally:
        permissoes.limpar_sessao(state.session_id)


async def test_so_desta_vez_libera_uma_candidatura_e_so_uma(provedor):
    state = _state("uma-vez")
    permissoes.responder("browser_comunicar", "uma_vez", state.session_id)
    primeira = await _despachar("clicar", {"ref": "candidatar"}, "comunicar", state)
    segunda = await _despachar("clicar", {"ref": "candidatar-outra"}, "comunicar", state)
    assert primeira["ok"] is True
    assert segunda.get("precisa_permissao") is True
    assert provedor.acoes == ["clicar"]


@pytest.mark.parametrize("texto,achado", [
    ("123.456.789-09", ["cpf"]),
    ("4111 1111 1111 1111", ["cartao"]),
    ("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz", ["credencial"]),
])
async def test_digitar_dado_sensivel_digita_e_avisa(provedor, texto, achado):
    """Dado sensivel se avisa, nao se pergunta. Digitar nao
    e enviar; o clique que envia (candidatar, pagar) continua perguntando."""
    resultado = await _despachar("digitar", {"ref": "campo", "texto": texto}, "nenhuma")
    assert resultado["ok"] is True and not resultado.get("precisa_permissao")
    assert resultado["dado_sensivel"] == achado
    assert "sem repetir o valor" in resultado["aviso"]


@pytest.mark.parametrize("texto", ["maria.silva@exemplo.com", "(11) 98765-4321"])
async def test_email_e_telefone_nem_avisam(provedor, texto):
    resultado = await _despachar("digitar", {"ref": "campo", "texto": texto}, "nenhuma")
    assert resultado["ok"] is True and "aviso" not in resultado


async def test_digitar_busca_comum_nao_pergunta(provedor):
    resultado = await _despachar("digitar", {"ref": "busca", "texto": "backend junior python"}, "nenhuma")
    assert resultado["ok"] is True


def test_contornar_seguranca_e_recusado_no_gate():
    decisao = portao.evaluate(
        _state(), "browser", "entra",
        {"acao": "clicar", "argumentos": {"ref": "prosseguir-mesmo-assim"}, "consequencia": "contornar_seguranca"},
    )
    assert decisao.allowed is False and "pessoa faz" in decisao.reason


def test_consequencia_inventada_e_recusada_no_gate():
    decisao = portao.evaluate(
        _state(), "browser", "entra",
        {"acao": "clicar", "argumentos": {"ref": "x"}, "consequencia": "talvez"},
    )
    assert decisao.allowed is False and "consequência desconhecida" in decisao.reason


def test_a_consequencia_atravessa_o_despacho():
    """O despacho reavalia o gate com `para_dict`; sem ela ali, sumia."""
    action = Action.de_argumentos({"acao": "clicar", "argumentos": {"ref": "x"}, "consequencia": "comunicar"})
    assert Action.de_argumentos(action.para_dict()).consequencia == "comunicar"
