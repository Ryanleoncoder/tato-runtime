"""Testa as ferramentas MCP, aprovações e cancelamento de turnos.

Verifica que capturas retornam como imagem e ações recusadas não executam.
"""
import asyncio

import pytest

from tato.mcp.servidor_mcp import Servidor
from tato.computador import computador
from tests.test_computador import Falso


@pytest.fixture(autouse=True)
async def _fecha_os_servidores(monkeypatch):
    """O turno de cada servidor fecha no fim do teste: a task não sobra para o loop seguinte."""
    abertos = []
    original = Servidor.__init__

    def guardar(self, *a, **k):
        original(self, *a, **k)
        abertos.append(self)

    monkeypatch.setattr(Servidor, "__init__", guardar)
    yield
    for servidor in abertos:
        await servidor.fechar()
    from tato.computador.computador_moldura import usar_agente

    usar_agente("Tato")


@pytest.fixture
def falso(monkeypatch):
    monkeypatch.setattr(computador, "_ESPERA_DEPOIS_DO_GESTO", 0)
    backend = Falso()
    computador.usar_backend(backend)
    yield backend
    computador._limpar_para_testes()
    computador.usar_backend(None)


class Cliente:
    """O lado do agente: manda pedidos e responde às perguntas de aprovação."""

    def __init__(self, *, pergunta=True, aprova=True, sessao="tato-teste", ocioso=30.0, **opcoes):
        self.enviadas = []
        self.aprova = aprova
        self.servidor = Servidor(self.enviadas.append, sessao=sessao, ocioso=ocioso, **opcoes)
        self.pergunta = pergunta
        self.perguntas = 0
        self._ids = iter(range(100, 1000))

    async def pedir(self, metodo, params=None):
        ident = next(self._ids)
        tarefa = asyncio.create_task(self.servidor.receber(
            {"jsonrpc": "2.0", "id": ident, "method": metodo, "params": params or {}}))
        while not tarefa.done():
            await asyncio.sleep(0.01)
            for m in list(self.enviadas):
                if m.get("method") == "elicitation/create":
                    self.enviadas.remove(m)
                    self.perguntas += 1
                    conteudo = {"action": "accept", "content": {"aprovar": True}} if self.aprova else {"action": "decline"}
                    await self.servidor.receber({"jsonrpc": "2.0", "id": m["id"], "result": conteudo})
        await tarefa
        return next(m for m in self.enviadas if m.get("id") == ident)

    async def iniciar(self, cliente="claude-code"):
        capacidades = {"elicitation": {}} if self.pergunta else {}
        return await self.pedir("initialize", {"protocolVersion": "2025-06-18", "capabilities": capacidades,
                                               "clientInfo": {"name": cliente, "version": "1"}})

    async def chamar(self, nome, argumentos, progresso=None):
        params = {"name": nome, "arguments": argumentos}
        if progresso is not None:
            params["_meta"] = {"progressToken": progresso}
        return (await self.pedir("tools/call", params))["result"]


async def test_lista_as_ferramentas_com_o_esquema():
    cliente = Cliente()
    await cliente.iniciar()
    ferramentas = {f["name"]: f for f in (await cliente.pedir("tools/list"))["result"]["tools"]}
    assert set(ferramentas) == {"computador", "browser", "gravacao"}
    assert "abrir" in ferramentas["computador"]["inputSchema"]["properties"]["acao"]["enum"]
    assert ferramentas["computador"]["inputSchema"]["properties"]["ritmo"]["enum"] == [
        "automatico", "natural", "rapido", "instantaneo"]


async def test_as_regras_gerais_vao_nas_instrucoes_e_as_descricoes_ficam_curtas():
    cliente = Cliente()
    inicio = (await cliente.iniciar())["result"]
    assert "`pedir_a_pessoa`" in inicio["instructions"] and "`dizer`" in inicio["instructions"]
    ferramentas = {f["name"]: f for f in (await cliente.pedir("tools/list"))["result"]["tools"]}
    for nome, ferramenta in ferramentas.items():
        assert len(ferramenta["description"]) < 3000, nome
        assert "ler_pagina" not in ferramenta["description"]
    # Quem ignora as instruções ainda sabe passar o login para a pessoa.
    assert "pedir_a_pessoa" in ferramentas["computador"]["description"]


async def test_o_print_volta_como_imagem(falso):
    cliente = Cliente()
    await cliente.iniciar()
    resultado = await cliente.chamar("computador", {"acao": "ver"})
    tipos = [p["type"] for p in resultado["content"]]
    assert tipos == ["text", "image"] and resultado["content"][1]["mimeType"] == "image/png"
    assert "print_anexado" in resultado["content"][0]["text"]
    assert not resultado["isError"] and falso.gestos == []


async def test_o_gesto_pergunta_pelo_cliente_e_o_turno_segue_aprovado(falso):
    from tato.permissoes import limpar_sessao

    limpar_sessao("tato-aprova")
    cliente = Cliente(sessao="tato-aprova")
    await cliente.iniciar()
    primeiro = await cliente.chamar("computador", {"acao": "clicar", "x": 10, "y": 10})
    assert '"executado": true' in primeiro["content"][0]["text"] and cliente.perguntas == 1
    await cliente.chamar("computador", {"acao": "clicar", "x": 20, "y": 20})
    assert len([g for g in falso.gestos if g[0] == "botao" and g[2]]) == 2, "os dois cliques rodaram"
    assert cliente.perguntas == 1, "o segundo gesto do turno não pergunta de novo"


async def test_recusado_nao_mexe(falso):
    from tato.permissoes import limpar_sessao

    limpar_sessao("tato-recusa")
    cliente = Cliente(sessao="tato-recusa", aprova=False)
    await cliente.iniciar()
    resultado = await cliente.chamar("computador", {"acao": "clicar", "x": 10, "y": 10})
    assert resultado["isError"] and "não aprovou" in resultado["content"][0]["text"]
    assert falso.gestos == []


async def test_sem_pergunta_no_cliente_nada_se_mexe(falso):
    from tato.permissoes import limpar_sessao

    limpar_sessao("tato-mudo")
    cliente = Cliente(sessao="tato-mudo", pergunta=False)
    await cliente.iniciar()
    resultado = await cliente.chamar("computador", {"acao": "clicar", "x": 10, "y": 10})
    assert resultado["isError"] and falso.gestos == []
    texto = resultado["content"][0]["text"]
    assert "não sabe perguntar" in texto and "--aprovacao-no-cliente" in texto and "não aprovou" not in texto


async def test_o_turno_parado_fecha_e_a_moldura_sai(falso):
    from tato.permissoes import limpar_sessao

    limpar_sessao("tato-ocioso")
    cliente = Cliente(sessao="tato-ocioso", ocioso=0.05)
    await cliente.iniciar()
    await cliente.chamar("computador", {"acao": "clicar", "x": 10, "y": 10})
    assert any(q.aberta for q in falso.quadros)
    await asyncio.sleep(0.3)
    assert not falso.quadros[-1].aberta


async def test_a_politica_vale_antes_de_executar():
    cliente = Cliente()
    await cliente.iniciar()
    resultado = await cliente.chamar("browser", {"acao": "navegar", "argumentos": {"url": "http://169.254.169.254/"}})
    assert resultado["isError"] and "recusado" in resultado["content"][0]["text"]


class TestQuemEstaUsando:
    @pytest.mark.parametrize("nome,esperado", [("claude-code", "Claude Code"), ("codex-mcp-client", "Codex"),
                                               ("meu_agente-local", "Meu Agente Local"), ("", "Agente")])
    def test_o_nome_do_cliente_como_a_pessoa_conhece(self, nome, esperado):
        from tato.computador.computador_moldura import nome_do_cliente

        assert nome_do_cliente(nome) == esperado

    async def test_a_moldura_diz_o_cliente_que_conectou(self, falso):
        from tato.permissoes import limpar_sessao

        limpar_sessao("tato-nome")
        cliente = Cliente(sessao="tato-nome")
        await cliente.iniciar("claude-code")
        await cliente.chamar("computador", {"acao": "clicar", "x": 10, "y": 10})
        quadro = next(q for q in falso.quadros if q.aberta)
        assert quadro.agente == "Claude Code" and quadro.selo.startswith("Claude Code está usando o computador")

    async def test_sem_nome_e_agente(self, falso):
        from tato.permissoes import limpar_sessao

        limpar_sessao("tato-sem-nome")
        cliente = Cliente(sessao="tato-sem-nome")
        await cliente.iniciar("")
        await cliente.chamar("computador", {"acao": "clicar", "x": 10, "y": 10})
        assert next(q for q in falso.quadros if q.aberta).selo.startswith("Agente está usando o computador")


class TestProgressoECancelamento:
    async def test_cada_passo_vai_ao_cliente_como_progresso(self, falso):
        from tato.permissoes import limpar_sessao

        limpar_sessao("tato-progresso")
        cliente = Cliente(sessao="tato-progresso")
        await cliente.iniciar()
        await cliente.chamar("computador", {"acao": "clicar", "x": 10, "y": 10}, progresso="p1")
        mensagens = [m["params"]["message"] for m in cliente.enviadas if m.get("method") == "notifications/progress"
                     and m["params"]["progressToken"] == "p1"]
        assert mensagens[0] == "computador: começando"
        assert "clicar: rodando" in mensagens and "clicar: ok" in mensagens

    async def test_o_cancelamento_para_o_gesto_e_tira_a_moldura(self, falso, monkeypatch):
        import threading

        from tato.permissoes import limpar_sessao

        limpar_sessao("tato-cancela")
        cliente = Cliente(sessao="tato-cancela")
        await cliente.iniciar()
        await cliente.chamar("computador", {"acao": "clicar", "x": 1, "y": 1})  # aprova o turno
        comecou, solta = threading.Event(), threading.Event()
        escrever = falso.escrever

        def escreve_devagar(texto):
            comecou.set()
            solta.wait(5)
            escrever(texto)

        monkeypatch.setattr(falso, "escrever", escreve_devagar)
        tarefa = asyncio.create_task(cliente.servidor.receber(
            {"jsonrpc": "2.0", "id": 77, "method": "tools/call",
             "params": {"name": "computador", "arguments": {"acao": "digitar", "texto": "oi"}}}))
        while not comecou.is_set():
            await asyncio.sleep(0.01)
        await cliente.servidor.receber({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                        "params": {"requestId": 77}})
        solta.set()
        await asyncio.gather(tarefa, return_exceptions=True)
        assert not any(m.get("id") == 77 for m in cliente.enviadas), "cancelado não recebe resposta"
        assert not falso.quadros[-1].aberta, "a moldura saiu, como no atalho de parar"


async def test_com_aprovacao_no_cliente_nao_pergunta_de_novo(falso):
    """O cliente declarou elicitation, mas quem registrou disse que ele já pergunta
    antes de cada chamada: vale essa, sem uma segunda pergunta por cima."""
    from tato.permissoes import limpar_sessao

    limpar_sessao("tato-no-cliente")
    cliente = Cliente(sessao="tato-no-cliente", aprova=False, aprovacao_no_cliente=True)
    await cliente.iniciar()
    resultado = await cliente.chamar("computador", {"acao": "clicar", "x": 10, "y": 10})
    assert not resultado["isError"] and cliente.perguntas == 0
    assert any(g[0] == "botao" for g in falso.gestos)
