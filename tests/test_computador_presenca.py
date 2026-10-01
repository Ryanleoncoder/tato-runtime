"""Testa a sinalização visual e a posse do computador durante uma sessão.

Confere leituras, gestos e a alternância entre agente e pessoa.
"""
import asyncio

import pytest

from tato.emissor import EMISSOR
from tato.computador import computador
from tests.test_computador import Falso, _aprovar, _no_turno, _state
from tests.test_computador_elementos import ComAcoes


@pytest.fixture
def falso(monkeypatch):
    monkeypatch.setattr(computador, "_ESPERA_DEPOIS_DO_GESTO", 0)
    backend = Falso()
    computador.usar_backend(backend)
    yield backend
    computador._limpar_para_testes()
    computador.usar_backend(None)


@pytest.fixture
def acoes(monkeypatch):
    monkeypatch.setattr(computador, "_ESPERA_DEPOIS_DO_GESTO", 0)
    backend = ComAcoes()
    computador.usar_backend(backend)
    yield backend
    computador._limpar_para_testes()
    computador.usar_backend(None)


def _ouvindo(eventos):
    return EMISSOR.set(eventos.append)


class TestComecoDoTurno:
    async def test_moldura_e_cartao_so_na_primeira_acao(self, falso):
        eventos = []

        async def turno():
            _ouvindo(eventos)
            assert falso.quadros == []
            resultado = await computador.executar({"acao": "ver"}, _state())
            return resultado, falso.quadros[-1].aberta

        resultado, aberta = await _no_turno(turno())
        assert resultado["ok"] and aberta
        assert {"type": "computador", "fase": "sessao", "status": "observando", "retomada": False} in eventos
        assert falso.gestos == [] and falso.prints == 1
        assert computador.sessao_ativa("s1"), "a primeira ação vincula a sessão à conversa"
        await asyncio.sleep(0)  # o fim da task do turno roda na volta seguinte do laço
        assert not falso.quadros[-1].aberta, "a moldura fecha com o turno"

    async def test_sem_a_funcao_nada_abre(self, falso):
        async def turno():
            return await computador.executar({"acao": "ver"}, _state(ligada=False))

        resultado = await _no_turno(turno())
        assert "@computador" in resultado["erro"]
        assert falso.quadros == []

    async def test_com_a_vez_da_pessoa_nada_abre(self, falso):
        from tato.computador import computador_vez

        computador_vez.passar_para_pessoa("s1", "login")

        async def turno():
            return await computador.executar({"acao": "ver"}, _state())

        resultado = await _no_turno(turno())
        assert not resultado.get("ok")
        assert falso.quadros == []

    async def test_com_outra_conversa_no_computador_nada_abre(self, falso):
        await _no_turno(computador.executar({"acao": "ver"}, _state("a")))
        await asyncio.sleep(0)
        antes = len(falso.quadros)

        async def turno():
            return await computador.executar({"acao": "ver"}, _state("b"))

        resultado = await _no_turno(turno())
        assert not resultado.get("ok")
        assert len(falso.quadros) == antes


class TestLeituraAparece:
    async def test_ler_vira_passo_no_cartao_e_fala_na_moldura(self, acoes):
        eventos = []
        state = _state()
        state.processo_do_turno = [{"ferramenta": "computador", "status": "running"}]  # sem fala do modelo

        async def turno():
            _ouvindo(eventos)
            r = await computador.executar({"acao": "elementos"}, state)
            return r, [f.texto for f in acoes.quadros[-1].falas]

        r, falas = await _no_turno(turno())
        gestos = [(e["titulo"], e["status"]) for e in eventos if e.get("fase") == "gesto"]
        assert r["ok"] and gestos == [("ler a janela", "rodando"), ("ler a janela", "ok")]
        assert [e for e in eventos if e.get("fase") == "gesto"][-1]["leituras"] == 1
        assert falas == ["árvore de acessibilidade da janela em foco, sem print"]
        assert acoes.gestos == []

    async def test_ler_tambem_poe_o_balao_no_mouse(self, acoes):
        """Olhar aparece na tela: o balão enquanto lê e, depois, o que viu."""
        async def turno():
            await computador.executar({"acao": "elementos"}, _state())
            return [q.etiqueta for q in acoes.quadros if q.etiqueta]

        etiquetas = await _no_turno(turno())
        assert etiquetas[0] == "ler a janela"
        assert etiquetas[-1].startswith("ler a janela · ") and etiquetas[-1].endswith(" elementos")
        assert not any(q.clique for q in acoes.quadros), "ler não ganha a onda do clique"

    async def test_com_fala_do_modelo_a_moldura_fica_com_ela(self, acoes):
        state = _state()
        state.processo_do_turno = [{"ferramenta": "computador", "status": "running", "fala": "vou achar o Iniciar"}]

        async def turno():
            await computador.executar({"acao": "elementos"}, state)
            return [f.texto for f in acoes.quadros[-1].falas]

        assert await _no_turno(turno()) == ["vou achar o Iniciar"]

    async def test_dizer_vai_para_a_caixinha_e_os_gestos_nao_o_empurram(self, acoes):
        """Pelo MCP não há fala do passo: o `dizer` é o que a pessoa lê, e os
        gestos seguintes sem `dizer` ficam só no balão."""
        state = _state()
        state.processo_do_turno = [{"ferramenta": "computador", "status": "running"}]

        async def turno():
            await computador.executar({"acao": "elementos", "dizer": "vou baixar o jogo pelo site do autor"}, state)
            for _ in range(4):
                await computador.executar({"acao": "elementos"}, state)
            await computador.executar({"acao": "esperar", "segundos": 0, "dizer": "esperando o download"}, state)
            return [f.texto for f in acoes.quadros[-1].falas]

        assert await _no_turno(turno()) == ["vou baixar o jogo pelo site do autor", "esperando o download"]


class TestSemMouseAparece:
    async def test_invocar_poe_balao_e_onda_sobre_o_elemento(self, acoes):
        _aprovar("s1")
        state = _state()

        async def turno():
            await computador.executar({"acao": "elementos"}, state)
            await computador.executar({"acao": "invocar", "elemento": 3}, state)
            return next(q for q in reversed(acoes.quadros) if q.etiqueta)

        quadro = await _no_turno(turno())
        assert quadro.etiqueta == "apertar · sem mouse" and quadro.clique
        assert quadro.ponto == acoes.caixa.centro
        assert not [g for g in acoes.gestos if g[0] in ("mover", "botao")]

    async def test_clique_comum_fica_onde_o_mouse_clicou(self, falso):
        """O ponto é o do gesto: se a pessoa mexer no mouse depois, balão e
        onda não vão atrás."""
        _aprovar("s1")

        async def turno():
            await computador.executar({"acao": "clicar", "x": 10, "y": 10}, _state())
            return next(q for q in reversed(falso.quadros) if q.etiqueta)

        assert (await _no_turno(turno())).ponto == falso.posicao_do_mouse()


class TestOndaEBarra:
    def test_a_onda_some_primeiro_e_o_balao_depois(self, monkeypatch):
        import time

        from tato.computador import computador_moldura
        from tato.computador.computador_moldura import BackendFalso, Moldura

        monkeypatch.setattr(computador_moldura, "ONDA_SEGUNDOS", 0.05)
        backend = BackendFalso()
        moldura = Moldura(backend, ao_parar=lambda: None).abrir()
        moldura.gesto("clicar", clique=True, ponto=(300, 200))
        assert backend.ultimo.clique and backend.ultimo.ponto == (300, 200)
        prazo = time.time() + 2
        while backend.ultimo.clique and time.time() < prazo:
            time.sleep(0.01)
        assert not backend.ultimo.clique and backend.ultimo.etiqueta == "clicar"
        monkeypatch.setattr(computador_moldura, "BALAO_SEGUNDOS", 0.05)
        moldura.gesto("rolar", ponto=(900, 500))
        prazo = time.time() + 2
        while backend.ultimo.etiqueta and time.time() < prazo:
            time.sleep(0.01)
        assert backend.ultimo.etiqueta == "" and backend.ultimo.ponto is None

    def test_onda_de_gesto_velho_nao_apaga_a_do_novo(self, monkeypatch):
        from tato.computador.computador_moldura import BackendFalso, Moldura

        backend = BackendFalso()
        moldura = Moldura(backend, ao_parar=lambda: None).abrir()
        moldura.gesto("clicar", clique=True, ponto=(1, 1))
        moldura.gesto("clicar duas vezes", clique=True, ponto=(2, 2))
        moldura._apagar(1, balao=False)
        moldura._apagar(1, balao=True)
        assert backend.ultimo.clique and backend.ultimo.ponto == (2, 2)
        assert backend.ultimo.etiqueta == "clicar duas vezes"

    def test_a_caixinha_fica_acima_da_barra_de_tarefas(self):
        from tato.computador.computador_desenho import desenhar
        from tato.computador.computador_moldura import BackendFalso, Moldura

        backend = BackendFalso()
        moldura = Moldura(backend, ao_parar=lambda: None).abrir()
        moldura.falar("abrindo o menu")
        _camada, sem_barra = desenhar((1920, 1080), backend.ultimo, (10, 10), brilho=False)
        _camada, com_barra = desenhar((1920, 1080), backend.ultimo, (10, 10), brilho=False, rodape=48)
        caixa = {r.nome: r for r in sem_barra}["caixinha"]
        acima = {r.nome: r for r in com_barra}["caixinha"]
        assert acima.y == caixa.y - 48 and acima.y + acima.altura <= 1080 - 48
        assert {r.nome: r for r in com_barra}["borda_baixo"].y + 4 == 1080, "a borda contorna a tela toda"


class TestFocar:
    """`focar` traz para a frente uma janela que abriu atrás da janela em foco."""

    async def test_traz_a_janela_e_o_cartao_mostra(self, falso, monkeypatch):
        focadas = []
        monkeypatch.setattr(falso, "focar", lambda titulo: focadas.append(titulo) or "Antigravity", raising=False)
        _aprovar("s1")
        eventos = []

        async def turno():
            _ouvindo(eventos)
            return await computador.executar({"acao": "focar", "janela": "antigravity"}, _state())

        r = await _no_turno(turno())
        assert r["ok"] and focadas == ["antigravity"] and "(Antigravity)" in r["feito"]
        assert ("trazer para a frente", "ok") in [(e["titulo"], e["status"]) for e in eventos if e.get("fase") == "gesto"]

    def test_a_janela_pedida_na_frente_confirma(self):
        """A prova do `focar` é a janela em foco: a lista pode nem ter base para comparar."""
        from tato.computador.computador import _janela_pedida_na_frente

        assert _janela_pedida_na_frente("focar", {"janela": "Extensões - Google Chrome"}, "Extensões - Google Chrome")
        assert _janela_pedida_na_frente("abrir", {"nome": "antigravity"}, "Antigravity - Welcome")
        assert not _janela_pedida_na_frente("focar", {"janela": "Chrome"}, "Visual Studio Code")
        assert not _janela_pedida_na_frente("clicar", {"janela": "Chrome"}, "Google Chrome")

    def test_sem_leitura_de_antes_nao_diz_que_nada_mudou(self):
        from tato.computador.computador_veredito import veredito

        r = veredito("clicar", {}, None, None, [])
        assert r["veredito"] == "nao_da_para_confirmar"

    async def test_sem_janela_nao_age(self, falso):
        r = await _no_turno(computador.executar({"acao": "focar"}, _state()))
        assert "`janela`" in r["erro"]

    async def test_sistema_sem_focar_aponta_a_barra(self, falso):
        _aprovar("s1")
        r = await _no_turno(computador.executar({"acao": "focar", "janela": "x"}, _state()))
        assert not r["ok"] and "barra de tarefas" in r["erro"]

    def test_a_ferramenta_oferece_o_gesto(self):
        import json
        import pathlib

        import tato

        ferramentas = json.loads((pathlib.Path(tato.__file__).parent / "ferramentas.json").read_text(encoding="utf-8"))
        propriedades = next(f for f in ferramentas if f["name"] == "computador")["parameters"]["properties"]
        assert "focar" in propriedades["acao"]["enum"] and "janela" in propriedades


class TestSequencia:
    """Gestos encadeados na mesma resposta rodam em ordem; um gesto sem efeito
    para o resto, que contava com ele."""

    async def test_gesto_sem_efeito_pede_para_parar_a_sequencia(self, acoes):
        _aprovar("s1")
        state = _state()

        async def turno():
            await computador.executar({"acao": "elementos"}, state)
            return await computador.executar({"acao": "clicar", "elemento": 4}, state)  # Entrar: nada muda

        r = await _no_turno(turno())
        assert r["veredito"] == "sem_efeito_aparente" and r["parar_sequencia"]


def test_titulo_do_documento_trocar_nao_vira_tela_nova():
    """Nova aba no Bloco de notas: só a aba nova aparece, não a janela inteira."""
    from tato.computador.computador_elementos import Elemento
    from tato.computador.computador_veredito import o_que_mudou

    def botao(nome, janela):
        return Elemento("botão", nome, 10, 10, 20, 20, janela=janela)

    antes = [botao("Fechar", "notas.txt - Bloco de notas"), botao("Minimizar", "notas.txt - Bloco de notas")]
    agora = [botao("Fechar", "Sem título - Bloco de notas"), botao("Minimizar", "Sem título - Bloco de notas"),
             Elemento("aba", "Sem título", 10, 10, 20, 20, janela="Sem título - Bloco de notas")]
    mudou = o_que_mudou(antes, agora)["mudou"]
    assert mudou.get("apareceram") == ["Sem título"] and not mudou.get("sumiram")


class TestDigitarNaJanelaCerta:
    """O teclado vai para a janela da frente: se ela não é mais a do gesto
    anterior, digitar escreveria no lugar errado."""

    async def test_janela_trocada_entre_gestos_recusa_sem_digitar(self, falso, monkeypatch):
        _aprovar("s1")
        frente = {"agora": "chrome.exe"}
        monkeypatch.setattr(falso, "programa_da_frente", lambda: frente["agora"], raising=False)

        async def turno():
            await computador.executar({"acao": "clicar", "x": 10, "y": 10}, _state())
            frente["agora"] = "code.exe"  # outro programa roubou o foco
            return await computador.executar({"acao": "digitar", "texto": "oi", "depois": "nada"}, _state())

        r = await _no_turno(turno())
        assert not r["ok"] and "outro programa veio para a frente" in r["erro"]
        assert not [g for g in falso.gestos if g[0] == "escrever"]

    async def test_atalho_do_sistema_passa_e_o_gesto_seguinte_define_a_frente(self, falso, monkeypatch):
        """O Executar do win+r aparece depois do atalho: digitar nele não é trocar de janela."""
        _aprovar("s1")
        frente = {"agora": "chrome.exe"}
        monkeypatch.setattr(falso, "programa_da_frente", lambda: frente["agora"], raising=False)

        async def turno():
            await computador.executar({"acao": "clicar", "x": 10, "y": 10}, _state())
            frente["agora"] = "code.exe"
            com_win = await computador.executar({"acao": "tecla", "tecla": "win+r", "depois": "nada"}, _state())
            frente["agora"] = "explorer.exe"
            no_executar = await computador.executar({"acao": "tecla", "tecla": "ctrl+a", "depois": "nada"}, _state())
            frente["agora"] = "code.exe"
            trocou = await computador.executar({"acao": "tecla", "tecla": "ctrl+v", "depois": "nada"}, _state())
            return com_win, no_executar, trocou

        com_win, no_executar, trocou = await _no_turno(turno())
        assert com_win["ok"] and no_executar["ok"]
        assert not trocou["ok"] and "outro programa veio para a frente" in trocou["erro"]

    async def test_programa_que_o_gesto_abriu_pode_receber_o_teclado(self, falso, monkeypatch):
        """Enter no Executar abre o mGBA um instante depois: programa novo passa,
        programa que já estava aberto é a pessoa trocando de janela."""
        _aprovar("s1")
        frente = {"agora": "explorer.exe", "janela": 10}
        monkeypatch.setattr(falso, "programa_da_frente", lambda: frente["agora"], raising=False)
        monkeypatch.setattr(falso, "janela_da_frente", lambda: frente["janela"], raising=False)
        monkeypatch.setattr(falso, "janelas_visiveis", lambda: {10, 20}, raising=False)

        async def turno():
            await computador.executar({"acao": "tecla", "tecla": "enter", "depois": "nada"}, _state())
            frente.update(agora="applicationframehost.exe", janela=30)  # app da Loja, janela nova
            novo = await computador.executar({"acao": "tecla", "tecla": "enter", "depois": "nada"}, _state())
            frente.update(agora="code.exe", janela=20)  # janela que já existia
            antigo = await computador.executar({"acao": "digitar", "texto": "oi", "depois": "nada"}, _state())
            return novo, antigo

        novo, antigo = await _no_turno(turno())
        assert novo["ok"]
        assert not antigo["ok"] and "outro programa veio para a frente" in antigo["erro"]

    async def test_janela_trocada_no_meio_para_e_conta_o_que_entrou(self, falso, monkeypatch):
        _aprovar("s1")
        leituras = {"n": 0}

        def frente():
            leituras["n"] += 1
            return "notepad.exe" if leituras["n"] <= 3 else "code.exe"

        monkeypatch.setattr(falso, "programa_da_frente", frente, raising=False)
        r = await _no_turno(computador.executar({"acao": "digitar", "texto": "abcdef", "depois": "nada"}, _state()))
        escritos = [g for g in falso.gestos if g[0] == "escrever"]
        assert not r["ok"] and "no meio da digitação" in r["erro"] and "de 6 caracteres" in r["erro"]
        assert 0 < len(escritos) < 6

    def test_encerrar_marca_o_turno_e_a_digitacao_para(self, falso):
        from tato.computador.computador_base import BackendDoComputador

        turno = computador._Turno(tarefa_id=1, state=_state())
        computador._encerrar("s-x", turno)
        assert turno.interrompido.is_set()
        falso.digitar("abc", interromper=turno.interrompido.is_set)
        assert not [g for g in falso.gestos if g[0] == "escrever"]
        assert issubclass(type(falso), BackendDoComputador)


def test_dois_processos_nao_pegam_o_computador_juntos(tmp_path):
    """A trava é do sistema: outro processo não pega enquanto este segura."""
    import os
    import subprocess
    import sys

    from tato.computador import computador_trava

    ambiente = {**os.environ, "TATO_HOME": str(tmp_path)}
    os.environ["TATO_HOME"], antes = str(tmp_path), os.environ.get("TATO_HOME")
    try:
        assert computador_trava.pegar() is None
        codigo = "from tato.computador import computador_trava as t; print(t.pegar() or 'PEGOU')"
        outro = subprocess.run([sys.executable, "-c", codigo], env=ambiente, capture_output=True, text=True,
                               encoding="utf-8")
        assert "outro agente está usando o computador" in outro.stdout
        computador_trava.soltar()
        outro = subprocess.run([sys.executable, "-c", codigo], env=ambiente, capture_output=True, text=True,
                               encoding="utf-8")
        assert "PEGOU" in outro.stdout
    finally:
        computador_trava.soltar()
        if antes is not None:
            os.environ["TATO_HOME"] = antes
        else:
            os.environ.pop("TATO_HOME", None)


def test_prazo_cresce_com_o_texto():
    from tato.prazo import prazo_de

    curto = prazo_de("computador", {"acao": "digitar", "texto": "oi"})
    longo = prazo_de("computador", {"acao": "digitar", "texto": "x" * 700})
    assert curto >= 60 and longo - curto >= 150


class TestNavegadorEscolhido:
    """Em qual Chrome navegar é da pessoa: vem uma vez, no primeiro resultado do turno."""

    async def test_vem_so_no_primeiro_resultado_do_turno(self, falso):
        async def turno():
            primeiro = await computador.executar({"acao": "janelas"}, _state())
            segundo = await computador.executar({"acao": "janelas"}, _state())
            return primeiro, segundo

        primeiro, segundo = await _no_turno(turno())
        assert "Chrome dela" in primeiro["navegador"] and "navegador" not in segundo

    def test_perfil_do_agente_com_o_nome_que_a_pessoa_deu(self, monkeypatch):
        from tato.computador import perfil_do_agente

        monkeypatch.setenv("TATO_NAVEGADOR_PERFIL", "agente")
        monkeypatch.setenv("TATO_NAVEGADOR_AGENTE_NOME", "Tatu")
        texto = perfil_do_agente.navegador_escolhido()
        assert '"Chrome do Tatu"' in texto and "sem as contas dela" in texto
        assert perfil_do_agente.pasta_do_perfil("Tatu Pesquisas").name == "chrome-tatu-pesquisas"
        assert '--window-name="Tatu"' in perfil_do_agente.argumentos("Tatu")


class TestProgramasDesenhados:
    """Jogo, editor e modelagem: sequência de teclas, recorte ampliado, esperar
    a tela mudar, arrastar com botão e modificador, e ler o texto sem print."""

    async def test_sequencia_roda_as_teclas_em_ordem(self, falso):
        _aprovar("s1")
        r = await _no_turno(computador.executar(
            {"acao": "tecla", "sequencia": ["direita", "direita", "z"], "pausa": 0, "depois": "nada"}, _state()))
        apertadas = [g[1] for g in falso.gestos if g[0] == "tecla" and g[2]]
        assert r["ok"] and apertadas == ["direita", "direita", "z"] and "3 de 3" in r["feito"]

    def test_sequencia_nao_esconde_combinacao_proibida(self):
        motivo = computador.trava("tecla", {"sequencia": ["direita", "alt+f4"]})
        assert motivo and "derruba" in motivo

    def test_sequencia_com_tecla_segurada_e_teto(self):
        assert computador._passos_da_sequencia({"sequencia": ["cima:0.6", {"tecla": "z", "segurar": 1}]}) == \
            [("cima", 0.6), ("z", 1.0)]
        assert "no máximo" in computador._contrato("tecla", {"sequencia": ["a"] * 41})

    async def test_arrastar_com_botao_do_meio_e_shift(self, falso):
        _aprovar("s1")
        await _no_turno(computador.executar({"acao": "arrastar", "x": 10, "y": 10, "para_x": 50, "para_y": 50,
                                             "botao": "meio", "com": "shift", "depois": "nada"}, _state()))
        ordem = [g for g in falso.gestos if g[0] in ("tecla", "botao")]
        assert ordem[0] == ("tecla", "shift", True) and ordem[1] == ("botao", "meio", True)
        assert ordem[-2] == ("botao", "meio", False) and ordem[-1] == ("tecla", "shift", False)

    def test_com_so_aceita_modificador(self):
        assert "ctrl, shift e alt" in computador._contrato("rolar", {"com": "a"})

    async def test_ver_com_regiao_amplia_so_o_pedaco(self, falso, monkeypatch):
        from tato.computador.computador_base import Tela

        pedidos = []

        def recorte(caixa):
            pedidos.append(caixa)
            return Tela(b"\x89PNG-recorte", 600, 300, 200, 100)

        monkeypatch.setattr(falso, "recorte", recorte, raising=False)
        state = _state()
        r = await _no_turno(computador.executar({"acao": "ver", "regiao": {"x": 100, "y": 50, "largura": 200,
                                                                              "altura": 100}}, state))
        assert r["ok"] and r["recorte"]["ampliado"] == 3.0 and pedidos
        assert state.print_da_tela and "recorte" in r["nota"]

    async def test_esperar_ate_mudar_volta_quando_a_tela_muda(self, falso, monkeypatch):
        quadros = iter([b"\x00" * 100] * 3 + [b"\xff" * 100])
        monkeypatch.setattr(computador, "_miniatura", lambda backend, caixa: next(quadros, b"\xff" * 100))
        r = await _no_turno(computador.executar({"acao": "esperar", "ate_mudar": True, "segundos": 5}, _state()))
        assert r["mudou"] is True and r["depois_de_s"] < 5

    async def test_esperar_ate_mudar_ignora_o_que_se_anima_sozinho(self, falso, monkeypatch):
        """Personagem parado balançando não é mudança; a tela trocar é."""
        parado, balanca = b"\x00" * 100, b"\xff" * 5 + b"\x00" * 95
        quadros = iter([parado, balanca, parado, balanca, parado, balanca])
        monkeypatch.setattr(computador, "_miniatura", lambda backend, caixa: next(quadros, parado))
        r = await _no_turno(computador.executar({"acao": "esperar", "ate_mudar": True, "segundos": 2}, _state()))
        assert r["mudou"] is False and "animacao_ignorada" in r

        quadros = iter([parado, balanca, parado, b"\x00" * 50 + b"\xff" * 50])
        monkeypatch.setattr(computador, "_miniatura", lambda backend, caixa: next(quadros, b"\x00" * 50 + b"\xff" * 50))
        r = await _no_turno(computador.executar({"acao": "esperar", "ate_mudar": True, "segundos": 2}, _state()))
        assert r["mudou"] is True

    async def test_ler_traz_o_texto_da_janela(self, falso, monkeypatch):
        monkeypatch.setattr(falso, "texto", lambda maximo=12000: "Pesquisa de preços\n- iPhone 17: R$ 8.299", raising=False)
        r = await _no_turno(computador.executar({"acao": "ler"}, _state()))
        assert r["ok"] and "R$ 8.299" in r["texto"] and not [g for g in falso.gestos if g[0] == "botao"]

    def test_ampliar_ate_tres_vezes(self):
        from PIL import Image

        from tato.computador.computador_base import ampliar

        tela = ampliar(Image.new("RGB", (100, 50)), 100, 50)
        assert (tela.largura, tela.altura) == (300, 150)


def test_procurar_nao_repete_o_mesmo_trecho():
    """Texto e link com as mesmas palavras aparecem duas vezes na árvore."""
    texto = "Download\nBaixe no itch.io\nFim\nDownload\nBaixe no itch.io\nFim"
    assert computador._trechos_com(texto, "itch") == ["Download | Baixe no itch.io | Fim"]


def test_ler_com_procurar_traz_so_as_linhas_do_termo():
    texto = "Filtros\niPhone 18 Pro 256 GB\nAntes: 11999 reais\n10799 reais\n\n10% OFF\nFrete grátis"
    trechos = computador._trechos_com(texto, "REAIS")
    assert trechos == ["iPhone 18 Pro 256 GB | Antes: 11999 reais | 10799 reais",
                       "Antes: 11999 reais | 10799 reais | 10% OFF"]


def test_procurar_com_mais_contexto_traz_o_nome_do_item():
    texto = "iPhone 18 Pro 256 GB\nApple\n5.0\nAntes: 11999 reais\n10799 reais"
    assert computador._trechos_com(texto, "10799", contexto=4)[0].startswith("iPhone 18 Pro 256 GB")
    assert computador.descrever("tecla", {"sequencia": ["a", "b"]}) == "sequência de 2 teclas"
