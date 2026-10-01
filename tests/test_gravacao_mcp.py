"""Contrato da gravação local exposta pelo MCP, sem capturar a tela real."""

import json
import threading
import uuid

import pytest
from PIL import Image, ImageDraw

from tato import gravacao_mcp
from tato.gravacao_mcp import _trechos
from tato.mcp.servidor_mcp import esquemas
from tato.mcp.servidor_mcp import Servidor


def test_gravacao_aparece_com_acoes_explicitas():
    ferramentas = {f["name"]: f for f in esquemas()}
    assert "gravacao" in ferramentas
    acoes = ferramentas["gravacao"]["inputSchema"]["properties"]["acao"]["enum"]
    assert {"iniciar", "parar", "estado", "ver", "montar"} <= set(acoes)


def test_trechos_validam_corte_e_borrao():
    trechos = _trechos({"trechos": [
        {"titulo": "Demonstração"},
        {"de": 1, "ate": 5, "velocidade": 2,
         "borrar": [{"de": 2, "ate": 3, "caixa": [1, 2, 20, 30]}]},
    ]}, 10)
    assert trechos[1]["pasta"] == "."
    assert trechos[1]["borrar"][0]["caixa"] == [1, 2, 20, 30]
    with pytest.raises(ValueError, match="90 s"):
        _trechos({"trechos": [{"de": 0, "ate": 100}]}, 100)
    assert _trechos({"trechos": [{"de": 0, "ate": 1, "legenda": "Um instante"}]}, 1)[0]["legenda"] == "Um instante"


@pytest.mark.asyncio
async def test_sem_aprovacao_gravacao_nao_comeca(monkeypatch, tmp_path):
    monkeypatch.setattr(gravacao_mcp, "pasta_do_tato", lambda: tmp_path)
    servidor = Servidor(lambda _: None, sessao="sem-permissao")
    try:
        resultado = await servidor._chamar("gravacao", {"acao": "iniciar"})
        assert resultado["isError"]
        assert "exige aprovação" in resultado["content"][0]["text"]
        assert not (tmp_path / "gravacoes").exists()
    finally:
        await servidor.fechar()


@pytest.mark.asyncio
async def test_iniciar_parar_e_fechar_conexao(monkeypatch, tmp_path):
    monkeypatch.setattr(gravacao_mcp, "pasta_do_tato", lambda: tmp_path)
    monkeypatch.setattr(gravacao_mcp, "_ativa", None)
    iniciou = threading.Event()

    def indicador_falso(parada, pronto, status, minutos):
        pronto.set()
        parada.wait(timeout=2)

    monkeypatch.setattr(gravacao_mcp, "_abrir_indicador", indicador_falso)

    def captura_falsa(pasta, fps, largura, caixa, minutos, *, parada, anunciar):
        assert not anunciar
        pasta.mkdir(parents=True)
        iniciou.set()
        parada.wait(timeout=2)
        (pasta / "quadros.json").write_text(json.dumps({"fim_ms": 100, "quadros": []}), encoding="utf-8")
        return {"quadros": 0, "duracao_s": 0.1, "pasta": str(pasta)}

    monkeypatch.setattr(gravacao_mcp, "gravar", captura_falsa)
    servidor = Servidor(lambda _: None, aprovacao_no_cliente=True, sessao="com-permissao")
    try:
        inicio = await servidor._chamar("gravacao", {"acao": "iniciar"})
        assert not inicio["isError"] and iniciou.wait(timeout=2)
        assert gravacao_mcp.estado("com-permissao")["gravando"]
        fim = await servidor._chamar("gravacao", {"acao": "parar"})
        assert not fim["isError"]
        assert gravacao_mcp.estado("com-permissao")["gravando"] is False
        assert gravacao_mcp.estado("com-permissao")["duracao_s"] == 0.1
        await servidor._chamar("gravacao", {"acao": "iniciar"})
        assert gravacao_mcp.estado("com-permissao")["gravando"]
    finally:
        await servidor.fechar()
    assert gravacao_mcp.estado("com-permissao")["gravando"] is False


def test_id_nao_aceita_caminho_arbitrario(monkeypatch, tmp_path):
    monkeypatch.setattr(gravacao_mcp, "pasta_do_tato", lambda: tmp_path)
    with pytest.raises(ValueError, match="inválido"):
        gravacao_mcp.montar_local({"id": "../../segredo"})


def test_sem_aviso_visivel_nao_captura(monkeypatch, tmp_path):
    monkeypatch.setattr(gravacao_mcp, "pasta_do_tato", lambda: tmp_path)
    monkeypatch.setattr(gravacao_mcp, "_ativa", None)
    chamadas = []
    monkeypatch.setattr(gravacao_mcp, "gravar", lambda *a, **k: chamadas.append(True))

    def aviso_falhou(parada, pronto, status, minutos):
        status["erro"] = "sem tela para mostrar o aviso"
        pronto.set()

    monkeypatch.setattr(gravacao_mcp, "_abrir_indicador", aviso_falhou)
    resultado = gravacao_mcp.iniciar("teste", {})
    assert not resultado["ok"] and "sem tela" in resultado["erro"]
    assert chamadas == [] and not (tmp_path / "gravacoes").exists()


def test_montar_reusa_gravacao_local_sem_escrever_no_stdout(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(gravacao_mcp, "pasta_do_tato", lambda: tmp_path)
    ident = "d4104ef3-b138-4856-ac0b-4eeed56da353"
    pasta = tmp_path / "gravacoes" / ident
    pasta.mkdir(parents=True)
    Image.new("RGB", (60, 40), "red").save(pasta / "00000.png")
    Image.new("RGB", (60, 40), "blue").save(pasta / "00001.png")
    (pasta / "quadros.json").write_text(json.dumps({"fim_ms": 1000, "quadros": [
        {"arquivo": "00000.png", "ms": 0}, {"arquivo": "00001.png", "ms": 500}]}), encoding="utf-8")
    resultado = gravacao_mcp.montar_local({"id": ident, "trechos": [
        {"titulo": "Teste"}, {"de": 0, "ate": 1, "borrar": [
            {"de": 0, "ate": 0.5, "caixa": [0, 0, 10, 10]}]}]})
    assert resultado["ok"] and (pasta / "resumo.gif").is_file()
    assert capsys.readouterr().out == "", "o MCP usa stdout para JSON-RPC"
    resumo, imagem = gravacao_mcp.ver_local({"id": ident, "amostras": 2})
    assert resumo["amostras"] == 2 and imagem.startswith("data:image/png;base64,")


def test_ver_mudancas_mostra_pares_do_piscar_sem_enviar_todos_os_quadros(monkeypatch, tmp_path):
    monkeypatch.setattr(gravacao_mcp, "pasta_do_tato", lambda: tmp_path)
    ident = str(uuid.uuid4())
    pasta = tmp_path / "gravacoes" / ident
    pasta.mkdir(parents=True)
    quadros = []
    for i in range(20):
        img = Image.new("RGB", (100, 80), "white")
        if i == 10:
            ImageDraw.Draw(img).rectangle((10, 10, 60, 60), fill="black")
        nome = f"{i:05d}.png"
        img.save(pasta / nome)
        quadros.append({"arquivo": nome, "ms": i * 100})
    (pasta / "quadros.json").write_text(json.dumps({"fim_ms": 40000, "quadros": quadros}), encoding="utf-8")
    resumo, prancha = gravacao_mcp.ver_local({"id": ident, "modo": "mudancas", "de": 0,
                                              "ate": 2, "amostras": 4,
                                              "area": [0, 0, 70, 70]})
    indices = [q["indice"] for q in resumo["quadros"]]
    assert {9, 10, 11} <= set(indices)
    assert len(indices) <= 4 and prancha.startswith("data:image/png;base64,")
    sem_mudanca, _ = gravacao_mcp.ver_local({"id": ident, "modo": "mudancas", "de": 0,
                                             "ate": 2, "amostras": 1,
                                             "area": [70, 0, 100, 80]})
    assert sem_mudanca["mudancas_detectadas"] == 0
    assert sem_mudanca["amostras"] == 1 and "rápido" in sem_mudanca["aviso"]
    with pytest.raises(ValueError, match="até 30 s"):
        gravacao_mcp.ver_local({"id": ident, "modo": "mudancas", "de": 0, "ate": 31})


def test_montar_junta_gravacoes_locais_e_legenda(monkeypatch, tmp_path):
    monkeypatch.setattr(gravacao_mcp, "pasta_do_tato", lambda: tmp_path)
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    for ident, cor in zip(ids, ("red", "blue")):
        pasta = tmp_path / "gravacoes" / ident
        pasta.mkdir(parents=True)
        Image.new("RGB", (120, 80), cor).save(pasta / "00000.png")
        (pasta / "quadros.json").write_text(json.dumps({"fim_ms": 1000,
            "quadros": [{"arquivo": "00000.png", "ms": 0}]}), encoding="utf-8")
    resultado = gravacao_mcp.montar_local({"id": ids[0], "trechos": [
        {"de": 0, "ate": 1, "legenda": "Primeiro trecho"},
        {"id": ids[1], "de": 0, "ate": 1, "legenda": "Segundo trecho"}]})
    assert resultado["ok"] and resultado["quadros"] == 2
    roteiro = json.loads((tmp_path / "gravacoes" / ids[0] / "roteiro.json").read_text(encoding="utf-8"))
    assert roteiro[1]["pasta"] == f"../{ids[1]}" and roteiro[1]["legenda"] == "Segundo trecho"
    with pytest.raises(ValueError, match="id de gravação inválido"):
        gravacao_mcp.montar_local({"id": ids[0], "trechos": [{"id": "../../fora", "de": 0, "ate": 1}]})


def test_indice_nao_pode_apontar_para_arquivo_fora_da_gravacao(monkeypatch, tmp_path):
    monkeypatch.setattr(gravacao_mcp, "pasta_do_tato", lambda: tmp_path)
    ident = "d4104ef3-b138-4856-ac0b-4eeed56da353"
    pasta = tmp_path / "gravacoes" / ident
    pasta.mkdir(parents=True)
    (pasta / "quadros.json").write_text(json.dumps({"fim_ms": 1000,
        "quadros": [{"arquivo": "../../outro.png", "ms": 0}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="índice"):
        gravacao_mcp.ver_local({"id": ident})


@pytest.mark.asyncio
async def test_ver_entrega_miniaturas_como_imagem_mcp(monkeypatch, tmp_path):
    monkeypatch.setattr(gravacao_mcp, "pasta_do_tato", lambda: tmp_path)
    ident = "d4104ef3-b138-4856-ac0b-4eeed56da353"
    pasta = tmp_path / "gravacoes" / ident
    pasta.mkdir(parents=True)
    Image.new("RGB", (40, 30), "green").save(pasta / "00000.png")
    (pasta / "quadros.json").write_text(json.dumps({"fim_ms": 100,
        "quadros": [{"arquivo": "00000.png", "ms": 0}]}), encoding="utf-8")
    servidor = Servidor(lambda _: None, sessao="ver-gravacao")
    try:
        resposta = await servidor._chamar("gravacao", {"acao": "ver", "id": ident})
        assert not resposta["isError"]
        assert [parte["type"] for parte in resposta["content"]] == ["text", "image"]
        assert resposta["content"][1]["mimeType"] == "image/png"
    finally:
        await servidor.fechar()


def test_montar_com_nome_nao_sobrescreve_a_anterior(monkeypatch, tmp_path):
    monkeypatch.setattr(gravacao_mcp, "pasta_do_tato", lambda: tmp_path)
    ident = str(uuid.uuid4())
    pasta = tmp_path / "gravacoes" / ident
    pasta.mkdir(parents=True)
    Image.new("RGB", (60, 40), "red").save(pasta / "00000.png")
    (pasta / "quadros.json").write_text(json.dumps({"fim_ms": 1000,
        "quadros": [{"arquivo": "00000.png", "ms": 0}]}), encoding="utf-8")
    gravacao_mcp.montar_local({"id": ident})
    resultado = gravacao_mcp.montar_local({"id": ident, "nome": "../Office e Word"})
    assert resultado["arquivo"].endswith("Office-e-Word.gif")
    assert (pasta / "resumo.gif").is_file() and (pasta / "Office-e-Word.gif").is_file()


def test_esperas_sem_area_ignoram_relogio_da_barra(tmp_path):
    from tato.gravacao import _trecho

    quadros = []
    for i in range(6):
        img = Image.new("RGB", (100, 100), "white")
        # Só a barra de baixo muda, como o relógio.
        ImageDraw.Draw(img).rectangle((0, 95, 100, 100), fill=(i * 40, 0, 0))
        img.save(tmp_path / f"{i:05d}.png")
        quadros.append({"arquivo": f"{i:05d}.png", "ms": i * 1000})
    (tmp_path / "quadros.json").write_text(json.dumps({"fim_ms": 6000, "quadros": quadros}), encoding="utf-8")
    pedaco = _trecho(tmp_path, 0, None, 400, 1, parado=0.01)
    assert sum(ms for _, ms in pedaco) <= 1000 + 400, "a tela parada vira uma espera curta"
