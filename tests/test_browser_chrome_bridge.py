"""A ponte nunca aceita página web nem ID de aba vindo do agente."""
import asyncio
from types import SimpleNamespace

from pathlib import Path

import pytest

import tato
from tato.navegador.browser_chrome_bridge import PonteChrome
from tato.navegador import browser_registry
from tato.navegador.browser_chrome_provider import ChromeTabProvider
from tato.navegador.browser_fake_provider import FakeBrowserProvider

_EXTENSAO = Path(tato.__file__).parent / "extensao"


class SocketFalso:
    def __init__(self, origem="chrome-extension://" + "a" * 32, host="127.0.0.1"):
        self.headers = {"origin": origem}
        self.client = SimpleNamespace(host=host)
        self.recebidas = asyncio.Queue()
        self.enviadas = asyncio.Queue()
        self.aceita = False
        self.fechada = None

    async def accept(self):
        self.aceita = True

    async def close(self, code):
        self.fechada = code

    async def receive_json(self):
        return await self.recebidas.get()

    async def send_json(self, value):
        await self.enviadas.put(value)


@pytest.mark.parametrize("origem,host", [
    ("https://site-malicioso.example", "127.0.0.1"),
    ("chrome-extension://" + "a" * 32, "192.0.2.4"),
])
async def test_rejeita_origem_ou_maquina_nao_local(origem, host):
    ponte = PonteChrome()
    socket = SocketFalso(origem, host)
    await ponte.atender(socket)
    assert socket.fechada == 1008 and not socket.aceita


async def test_rejeita_codigo_errado():
    ponte = PonteChrome()
    ponte.codigo_de_pareamento()
    socket = SocketFalso()
    await socket.recebidas.put({"kind": "pair", "secret": "errado"})
    await ponte.atender(socket)
    assert socket.fechada == 1008 and not ponte.conectada


async def test_a_extensao_do_tato_conecta_sem_codigo(tmp_path):
    """O ID fixo (a `key` do manifest) basta; o token sai igual ao do código."""
    import json
    from pathlib import Path

    from tato.navegador.browser_chrome_bridge import ID_DA_EXTENSAO

    manifesto = json.loads(((_EXTENSAO / "manifest.json")).read_text(encoding="utf-8"))
    assert manifesto["key"] and manifesto["name"] == "Tato"
    ponte = PonteChrome(tmp_path / "pareamento.json")
    socket = SocketFalso(origem=f"chrome-extension://{ID_DA_EXTENSAO}")
    await socket.recebidas.put({"kind": "pair", "secret": ""})
    servindo = asyncio.create_task(ponte.atender(socket))
    try:
        ready = await asyncio.wait_for(socket.enviadas.get(), 1)
        assert ready["kind"] == "ready" and ready["token"] and ponte.conectada
    finally:
        servindo.cancel()
        with pytest.raises(asyncio.CancelledError):
            await servindo


async def test_outra_extensao_sem_codigo_e_recusada():
    ponte = PonteChrome()
    socket = SocketFalso()  # outro ID
    await socket.recebidas.put({"kind": "pair", "secret": ""})
    await ponte.atender(socket)
    assert socket.fechada == 1008 and not ponte.conectada


async def test_pareia_e_troca_comandos_sem_tab_id():
    ponte = PonteChrome()
    codigo = ponte.codigo_de_pareamento()["codigo"]
    socket = SocketFalso()
    await socket.recebidas.put({"kind": "pair", "secret": codigo})
    servindo = asyncio.create_task(ponte.atender(socket))
    try:
        ready = await asyncio.wait_for(socket.enviadas.get(), 1)
        assert ready["kind"] == "ready" and ready["token"]
        pedido = asyncio.create_task(ponte.chamar("execute", "conversa", action={"acao": "snapshot"}))
        comando = await asyncio.wait_for(socket.enviadas.get(), 1)
        assert comando["kind"] == "command" and comando["conversation"] == "conversa"
        assert "tabId" not in comando and "tab_id" not in comando
        await socket.recebidas.put({"kind": "result", "id": comando["id"], "ok": True,
                                    "result": {"fonte": "chrome_tab", "dados": {"arvore": []}}})
        assert (await pedido)["fonte"] == "chrome_tab"
        await socket.recebidas.put({"kind": "closed", "conversation": "conversa"})
        await asyncio.sleep(0)
        assert not ponte.aba_aberta("conversa")
    finally:
        servindo.cancel()
        with pytest.raises(asyncio.CancelledError):
            await servindo


async def test_reset_libera_apenas_a_conversa_fechada():
    ponte = PonteChrome()
    ponte._fechadas.add("a")
    ponte._fechadas.add("b")
    ponte._loop = asyncio.get_running_loop()

    # A limpeza do marcador acontece no retorno normal de `chamar`; o outro
    # marcador continua intacto mesmo que uma conversa seja reaberta.
    class Socket:
        async def send_json(self, message):
            futuro = ponte._pendentes[message["id"]]
            futuro.set_result({"ok": True, "result": {"reopened": True}})

    ponte._socket = Socket()
    assert (await ponte.chamar("reset", "a"))["reopened"]
    assert "a" not in ponte._fechadas and "b" in ponte._fechadas


def test_prefere_chrome_conectado_e_preserva_aba_no_shutdown(monkeypatch):
    browser_registry._limpar_para_testes()
    try:
        browser_registry.registrar(FakeBrowserProvider("tests/fixtures/browser/vagas.html"))
        browser_registry.registrar(ChromeTabProvider())
        monkeypatch.setattr(PonteChrome, "conectada", property(lambda self: True))
        assert browser_registry.resolver().name == "chrome_tabs"
        sessao, reutilizada = browser_registry._capability(browser_registry.resolver(), "conversa")
        assert not reutilizada and sessao.session_id == "chrome-tabs:conversa"
        assert browser_registry.encerrar_todos() == 0
        assert browser_registry.esta_aberta("conversa")
        assert browser_registry.fechar_ociosos(1, agora=10**12) == 0
        assert browser_registry.esta_aberta("conversa")
        monkeypatch.setattr(PonteChrome, "conectada", property(lambda self: False))
        assert browser_registry.resolver().name == "chrome_tabs"
    finally:
        browser_registry._limpar_para_testes()


async def test_o_pareamento_sobrevive_ao_reinicio_da_api(tmp_path):
    """Cada reinício pedia pareamento novo: o token só existia na memória.
    Agora o hash dele fica no disco, e o token nunca."""
    arquivo = tmp_path / "navegador_extensao.json"
    ponte = PonteChrome(arquivo)
    codigo = ponte.codigo_de_pareamento()["codigo"]
    socket = SocketFalso()
    await socket.recebidas.put({"kind": "pair", "secret": codigo})
    servindo = asyncio.create_task(ponte.atender(socket))
    try:
        token = (await asyncio.wait_for(socket.enviadas.get(), 1))["token"]
    finally:
        servindo.cancel()
        with pytest.raises(asyncio.CancelledError):
            await servindo
    assert token not in arquivo.read_text(encoding="utf-8")

    reiniciada = PonteChrome(arquivo)
    socket = SocketFalso()
    await socket.recebidas.put({"kind": "pair", "secret": token})
    servindo = asyncio.create_task(reiniciada.atender(socket))
    try:
        pronto = await asyncio.wait_for(socket.enviadas.get(), 1)
        assert pronto == {"kind": "ready", "token": token}
    finally:
        servindo.cancel()
        with pytest.raises(asyncio.CancelledError):
            await servindo
    assert not PonteChrome(arquivo)._autorizada("outro-token")


def test_o_fim_do_turno_apaga_a_borda_da_aba(monkeypatch):
    """A borda fica acesa o turno inteiro, pensando incluído; quem a apaga é o
    fim do turno, não um relógio de alguns segundos."""
    from types import SimpleNamespace

    from tato.navegador import browser_chrome_provider as provedor

    enviados = []
    monkeypatch.setattr(provedor.ponte_chrome, "aba_aberta", lambda conversa: True)
    monkeypatch.setattr(provedor.ponte_chrome, "enviar_sem_esperar",
                        lambda comando, conversa: enviados.append((comando, conversa)) or True)
    assert provedor.soltar_do_turno(SimpleNamespace(session_id="c1", navegador_do_turno={"passos": []}))
    assert enviados == [("soltar", "c1")]
    # Turno que não usou o navegador não mexe na aba.
    assert not provedor.soltar_do_turno(SimpleNamespace(session_id="c1", navegador_do_turno=None))
    assert enviados == [("soltar", "c1")]


def test_o_selo_saiu_e_a_extensao_mantem_a_borda_viva():
    from pathlib import Path

    script = (_EXTENSAO / "browser_sobreposicao.js").read_text(encoding="utf-8")
    assert "usando esta aba" not in script, "o aviso é a faixa do próprio Chrome"
    worker = (_EXTENSAO / "service-worker.js").read_text(encoding="utf-8")
    assert "toggleOverlay(tabId, 'vivo')" in worker, "sem o sinal de vida a borda apagaria entre as ações"


