"""Os elementos da tela: número sobre cada botão e campo, e o gesto pelo número.

Backend falso que descreve a janela como a acessibilidade descreveria. O
leitor de verdade do Linux está em test_computador_atspi.py (Xvfb + AT-SPI).
"""
import io
from dataclasses import replace

import pytest
from PIL import Image

from tato.computador import computador
from tato.computador.computador_base import ErroDoComputador, tela_de_imagem
from tato.computador.computador_elementos import MAX_ELEMENTOS, Elemento, filtrar
from tests.test_computador import Falso, _aprovar, _no_turno, _state, digitado

ENTRAR = Elemento("botao", "Entrar", 2000, 1200, 200, 80)
USUARIO = Elemento("campo", "Usuário", 900, 600, 700, 60)
SENHA = Elemento("senha", "Senha", 900, 700, 700, 60)


class ComElementos(Falso):
    def __init__(self):
        super().__init__()
        self.lista = [ENTRAR, USUARIO, SENHA]
        self.em_foco = None

    def tela(self):
        self.prints += 1
        return tela_de_imagem(Image.new("RGB", self._tamanho, (250, 250, 250)))

    def elementos(self):
        return list(self.lista)

    def foco(self):
        return self.em_foco


@pytest.fixture
def backend(monkeypatch):
    monkeypatch.setattr(computador, "_ESPERA_DEPOIS_DO_GESTO", 0)
    falso = ComElementos()
    computador.usar_backend(falso)
    yield falso
    computador._limpar_para_testes()
    computador.usar_backend(None)


class TestFiltrar:
    def test_ordem_de_leitura_sem_repetir_e_so_o_que_aparece(self):
        tamanho = (1000, 800)
        achados = filtrar([
            Elemento("botao", "Baixo", 10, 500, 50, 20),
            Elemento("botao", "Direita", 300, 100, 50, 20),
            Elemento("botao", "Esquerda", 10, 104, 50, 20),
            Elemento("botao", "Esquerda de novo", 10, 104, 50, 20),
            Elemento("botao", "Fora", 2000, 100, 50, 20),
            Elemento("botao", "Sem tamanho", 10, 10, 0, 0),
            Elemento("janela", "Papel desconhecido", 10, 10, 50, 20),
            Elemento("campo", "Metade fora", 980, 700, 100, 50),
        ], tamanho)
        assert [e.nome for e in achados] == ["Esquerda", "Direita", "Baixo", "Metade fora"]
        assert achados[-1].largura == 20, "o que passa da borda é cortado nela"

    def test_para_no_teto(self):
        muitos = [Elemento("item", f"linha {i}", 0, i * 10, 100, 8) for i in range(200)]
        assert len(filtrar(muitos, (1000, 4000))) == MAX_ELEMENTOS


class TestMarcar:
    async def test_ver_com_marcar_numera_e_devolve_a_lista(self, backend):
        state = _state()
        r = await _no_turno(computador.executar({"acao": "ver", "marcar": True}, state))
        assert [e["nome"] for e in r["elementos"]] == ["Usuário", "Senha", "Entrar"]
        entrar = r["elementos"][2]
        assert entrar["n"] == 3 and entrar["papel"] == "botão"
        # O centro vem no espaço do print (2560 → 1456).
        assert (entrar["x"], entrar["y"]) == (round(2100 * 1456 / 2560), round(1240 * 1456 / 2560))
        assert "elemento: n" in r["nota"]
        png = Image.open(io.BytesIO(__import__("base64").b64decode(state.print_da_tela["data_url"].split(",")[1])))
        borda = round(2000 * 1456 / 2560)
        assert any(png.getpixel((x, entrar["y"])) != (250, 250, 250) for x in range(borda - 2, borda + 3)), \
            "a caixa do elemento está desenhada no print"

    async def test_clicar_pelo_numero_vai_ao_centro(self, backend):
        _aprovar("s1")
        state = _state()

        async def turno():
            await computador.executar({"acao": "ver", "marcar": True}, state)
            return await computador.executar({"acao": "clicar", "elemento": 3}, state)

        r = await _no_turno(turno())
        assert r["ok"] and r["feito"] == "clicar no elemento 3"
        movimentos = [g for g in backend.gestos if g[0] == "mover"]
        x, y = movimentos[-1][1:]
        assert abs(x - 2100) <= 2 and abs(y - 1240) <= 2
        assert [e["nome"] for e in r["elementos"]] == ["Usuário", "Senha", "Entrar"], \
            "com marcar ligado no turno, o print depois do gesto também vem numerado"

    async def test_numero_sem_print_marcado_e_recusado(self, backend):
        r = await _no_turno(computador.executar({"acao": "clicar", "elemento": 3}, _state()))
        assert "marcar" in r["erro"] and backend.gestos == []

    async def test_numero_que_nao_e_numero(self, backend):
        r = await _no_turno(computador.executar({"acao": "clicar", "elemento": "entrar"}, _state()))
        assert "número" in r["erro"]

    async def test_sem_acessibilidade_segue_pela_coordenada(self, backend, monkeypatch):
        def sem(*_):
            raise ErroDoComputador("sem AT-SPI neste Linux")

        monkeypatch.setattr(backend, "elementos", sem)
        r = await _no_turno(computador.executar({"acao": "ver", "marcar": True}, _state()))
        assert r["ok"] and "elementos" not in r and "coordenada" in r["elementos_aviso"]

    async def test_janela_que_nao_se_descreve(self, backend):
        backend.lista = []
        r = await _no_turno(computador.executar({"acao": "ver", "marcar": True}, _state()))
        assert r["ok"] and "não descreve" in r["elementos_aviso"]


class TestDadoSensivel:
    """Senha não passa pelo teclado; e-mail e dado pessoal a pessoa confirma no cartão."""

    async def test_senha_nao_passa_pelo_teclado(self, backend):
        """O que o agente digita sai do modelo: senha iria à API do provedor."""
        _aprovar("s1")
        backend.em_foco = SENHA
        r = await _no_turno(computador.executar({"acao": "digitar", "texto": "hunter2"}, _state()))
        assert r["campo_de_senha"] and r["sem_apelacao"] and "preenchimento automático" in r["proximo"]
        assert "precisa_permissao" not in r and backend.gestos == []

    @pytest.mark.parametrize("tecla, recusa", [("a", True), ("shift+a", True), ("7", True),
                                               ("enter", False), ("tab", False), ("ctrl+v", False)])
    async def test_letra_solta_no_campo_de_senha_tambem_nao(self, backend, tecla, recusa):
        _aprovar("s1")
        backend.em_foco = SENHA
        r = await _no_turno(computador.executar({"acao": "tecla", "tecla": tecla}, _state()))
        assert bool(r.get("campo_de_senha")) is recusa, r

    async def test_e_mail_confirmado_digita_e_confirma_de_novo_na_proxima(self, backend):
        from tato.permissoes import limpar_sessao, responder

        limpar_sessao("s1")
        backend.em_foco = USUARIO
        state = _state()
        pedido = await _no_turno(computador.executar({"acao": "digitar", "texto": "ana@exemplo.com"}, state))
        assert pedido["precisa_permissao"] and pedido["so_desta_vez"] and pedido["chave"] == "computador_sensivel"
        assert "é isso mesmo" in pedido["motivo"]
        assert "ana@exemplo.com" not in pedido["comando"] + pedido["motivo"], "o cartão nunca mostra o valor"
        assert backend.gestos == []

        responder(pedido["comando"], "uma_vez", "s1")
        r = await _no_turno(computador.executar({"acao": "digitar", "texto": "ana@exemplo.com"}, state))
        assert r["ok"] and digitado(backend.gestos) == "ana@exemplo.com", \
            "a confirmação vale como a aprovação do turno"

        de_novo = await _no_turno(computador.executar({"acao": "digitar", "texto": "ana@exemplo.com"}, state))
        assert de_novo["precisa_permissao"], "a confirmação vale uma vez só"

    async def test_email_no_texto_ou_no_campo_pede_confirmacao(self, backend):
        from tato.permissoes import limpar_sessao

        limpar_sessao("s1")
        backend.em_foco = USUARIO
        r = await _no_turno(computador.executar({"acao": "digitar", "texto": "ana@exemplo.com"}, _state()))
        assert r["chave"] == "computador_sensivel" and "e-mail" in r["motivo"]
        backend.em_foco = Elemento("campo", "E-mail", 900, 600, 700, 60)
        r = await _no_turno(computador.executar({"acao": "digitar", "texto": "ana"}, _state()))
        assert r["chave"] == "computador_sensivel" and "E-mail" in r["motivo"]

    async def test_texto_comum_so_pede_a_aprovacao_do_turno(self, backend):
        from tato.permissoes import limpar_sessao

        limpar_sessao("s1")
        backend.em_foco = USUARIO
        r = await _no_turno(computador.executar({"acao": "digitar", "texto": "ana"}, _state()))
        assert r["precisa_permissao"] and r["chave"] == "computador"


class TestMenosPrint:
    """Com a janela se descrevendo, a conferência é a lista em texto, não o print."""

    async def test_elementos_lista_sem_print(self, backend):
        state = _state()
        r = await _no_turno(computador.executar({"acao": "elementos"}, state))
        assert [e["nome"] for e in r["elementos"]] == ["Usuário", "Senha", "Entrar"]
        assert backend.prints == 0 and getattr(state, "print_da_tela", None) is None

    async def test_depois_do_gesto_vem_a_lista_e_o_que_mudou(self, backend):
        _aprovar("s1")
        state = _state()

        async def turno():
            await computador.executar({"acao": "elementos"}, state)
            backend.lista = [ENTRAR, Elemento("botao", "Sair", 100, 100, 80, 40)]
            return await computador.executar({"acao": "clicar", "elemento": 3}, state)

        r = await _no_turno(turno())
        assert r["ok"] and "print" not in r and backend.prints == 0
        assert r["mudou"] == {"apareceram": ["Sair"], "sumiram": ["Usuário", "Senha"]}
        assert [e["nome"] for e in r["elementos"]] == ["Sair", "Entrar"], "os números passam a ser os da lista nova"

    async def test_sem_acessibilidade_volta_ao_print(self, backend, monkeypatch):
        def sem(*_):
            raise ErroDoComputador("sem AT-SPI")

        monkeypatch.setattr(backend, "elementos", sem)
        _aprovar("s1")
        r = await _no_turno(computador.executar({"acao": "clicar", "x": 10, "y": 10}, _state()))
        assert r["ok"] and r["print"] and backend.prints == 1

    async def test_depois_print_forca_o_print(self, backend):
        _aprovar("s1")
        r = await _no_turno(computador.executar({"acao": "clicar", "x": 10, "y": 10, "depois": "print"}, _state()))
        assert r["print"] and backend.prints == 1 and "elementos" not in r


class TestCamadas:
    """Menu aberto, janela em foco, barra e janelas de fundo numa lista só."""

    def _camada(self, tipo, nome, caixa, elementos, z=None):
        from tato.computador.computador_elementos import Camada

        return Camada(tipo, nome, *caixa, elementos, z)

    def test_o_que_outra_camada_cobre_sai(self):
        from tato.computador.computador_elementos import juntar

        menu = self._camada("menu", "menu", (100, 100, 200, 100), [Elemento("menu", "Copiar", 110, 110, 180, 20)])
        foco = self._camada("foco", "Editor", (0, 0, 800, 600), [
            Elemento("botao", "Salvar", 10, 10, 80, 30),
            Elemento("botao", "Embaixo do menu", 150, 150, 80, 30)])
        fundo = self._camada("janela", "Planilha", (700, 0, 500, 600), [
            Elemento("botao", "Coberto pelo editor", 720, 20, 50, 30),
            Elemento("botao", "Visível", 900, 20, 50, 30)])
        barra = self._camada("barra", "barra de tarefas", (0, 760, 1280, 40), [Elemento("botao", "Iniciar", 0, 760, 48, 40)])
        nomes = [(e.janela, e.nome) for e in juntar([fundo, barra, foco, menu])]
        assert nomes == [("menu", "Copiar"), ("Editor", "Salvar"), ("barra de tarefas", "Iniciar"),
                         ("Planilha", "Visível")]

    def test_com_a_pilha_conhecida_ela_manda(self):
        from tato.computador.computador_elementos import juntar

        de_cima = self._camada("janela", "A", (0, 0, 400, 400), [Elemento("botao", "de A", 10, 10, 50, 30)], z=1)
        de_baixo = self._camada("janela", "B", (200, 0, 400, 400), [
            Elemento("botao", "de B coberto", 250, 10, 50, 30), Elemento("botao", "de B livre", 500, 10, 50, 30)], z=2)
        assert [e.nome for e in juntar([de_baixo, de_cima])] == ["de A", "de B livre"]

    def test_sem_pilha_duas_de_fundo_que_se_cruzam_somem_no_cruzamento(self):
        from tato.computador.computador_elementos import juntar

        a = self._camada("janela", "A", (0, 0, 400, 400), [Elemento("botao", "a no cruzamento", 300, 10, 50, 30)])
        b = self._camada("janela", "B", (200, 0, 400, 400), [Elemento("botao", "b no cruzamento", 250, 10, 50, 30),
                                                            Elemento("botao", "b livre", 500, 10, 50, 30)])
        assert [e.nome for e in juntar([a, b])] == ["b livre"]

    def test_janela_de_fundo_entra_com_pouco(self):
        from tato.computador.computador_elementos import MAX_POR_JANELA_DE_FUNDO, juntar

        muitos = [Elemento("item", f"linha {i}", 0, i * 10, 100, 8) for i in range(50)]
        fundo = self._camada("janela", "Lista", (0, 0, 200, 600), muitos)
        assert len(juntar([fundo])) == MAX_POR_JANELA_DE_FUNDO

    def test_filtrar_mantem_a_ordem_das_janelas_e_o_modelo_ve_de_onde_veio(self):
        from tato.computador.computador_elementos import para_o_modelo

        menu = Elemento("menu", "Colar", 500, 500, 80, 20, janela="menu")
        foco = Elemento("botao", "Salvar", 10, 10, 80, 30, janela="Editor")
        achados = filtrar([menu, foco], (1280, 800))
        assert [e.nome for e in achados] == ["Colar", "Salvar"], "o menu vem antes mesmo estando mais abaixo"
        assert [i["janela"] for i in para_o_modelo(achados, 1.0)] == ["menu", "Editor"]
        assert "janela" not in para_o_modelo([foco], 1.0)[0], "uma janela só: não repete o nome"


class TestPilhaDoWindows:
    """A classificação da pilha do Windows, sem Windows."""

    def test_classifica_menu_popup_barra_foco_fundo_e_area_de_trabalho(self):
        from tato.computador.computador_uia import _WS_POPUP, camadas_da_pilha

        caixa = (0, 0, 100, 100)
        pilha = [
            (1, "#32768", "", _WS_POPUP, 50, caixa),                 # menu de contexto
            (2, "Shell_TrayWnd", "", 0, 60, caixa),                  # barra de tarefas
            (3, "Chrome_WidgetWin_1", "", _WS_POPUP, 70, caixa),     # lista aberta do app em foco
            (4, "tooltips_class32", "", _WS_POPUP, 80, caixa),       # dica de outro processo: não lê
            (5, "Chrome_WidgetWin_1", "Portal - Chrome", 0, 70, caixa),
            *[(10 + i, "Notepad", f"nota {i}", 0, 90 + i, caixa) for i in range(6)],
            (30, "Progman", "Program Manager", 0, 1, caixa),
        ]
        camadas = camadas_da_pilha(pilha, frente=5)
        assert [(t, n, h) for t, n, h, _c, _z in camadas] == [
            ("menu", "menu", 1), ("barra", "barra de tarefas", 2), ("menu", "menu", 3),
            ("foco", "Portal - Chrome", 5),
            ("janela", "nota 0", 10), ("janela", "nota 1", 11), ("janela", "nota 2", 12), ("janela", "nota 3", 13),
            ("janela", "área de trabalho", 30)]
        assert [z for *_r, z in camadas] == [0, 1, 2, 4, 5, 6, 7, 8, 11], "o z é a posição na pilha"


class ComAcoes(ComElementos):
    """Aperta e preenche como a acessibilidade faria, mudando a própria lista."""

    def __init__(self):
        super().__init__()
        self.caixa = Elemento("caixa", "Lembrar de mim", 900, 800, 300, 30, estado="desmarcada")
        self.lista = [USUARIO, SENHA, self.caixa, ENTRAR]
        self.acoes = []

    def invocar(self, alvo):
        self.acoes.append(("invocar", alvo.nome))
        if alvo.nome == "Lembrar de mim":
            marcada = replace(self.caixa, estado="marcada")
            self.lista = [marcada if e.nome == alvo.nome else e for e in self.lista]
        return "apertado"

    def focar_elemento(self, alvo):
        self.acoes.append(("focar_elemento", alvo.nome))
        self.em_foco = alvo
        self._tudo_selecionado = False

    def escolher_opcao(self, alvo, opcao):
        self.acoes.append(("escolher_opcao", alvo.nome, opcao))
        self.lista = [replace(e, valor=opcao) if e.nome == alvo.nome else e for e in self.lista]

    def combinacao(self, texto):
        super().combinacao(texto)
        if texto == "ctrl+a":
            self._tudo_selecionado = True

    def escrever(self, texto):
        # O campo em foco recebe a tecla; com tudo selecionado, a primeira
        # tecla troca o que havia.
        super().escrever(texto)
        if self.em_foco is None:
            return
        atual = next(e for e in self.lista if e.nome == self.em_foco.nome)
        valor = texto if getattr(self, "_tudo_selecionado", False) else atual.valor + texto
        self._tudo_selecionado = False
        self.lista = [replace(e, valor=valor) if e.nome == atual.nome else e for e in self.lista]


@pytest.fixture
def acoes(monkeypatch):
    monkeypatch.setattr(computador, "_ESPERA_DEPOIS_DO_GESTO", 0)
    falso = ComAcoes()
    computador.usar_backend(falso)
    yield falso
    computador._limpar_para_testes()
    computador.usar_backend(None)


async def _lido_e(acao, estado):
    async def turno():
        await computador.executar({"acao": "elementos"}, estado)
        return await computador.executar(acao, estado)

    return await _no_turno(turno())


class TestSemMouse:
    """`invocar` e `definir_valor`: agem no controle, o mouse fica onde está."""

    async def test_invocar_marca_a_caixa_e_o_veredito_confirma(self, acoes):
        _aprovar("s1")
        r = await _lido_e({"acao": "invocar", "elemento": 3}, _state())
        assert r["ok"] and r["veredito"] == "confirmado" and "marcada" in r["proximo"]
        assert acoes.acoes == [("invocar", "Lembrar de mim")]
        assert not [g for g in acoes.gestos if g[0] in ("mover", "botao")], "o mouse não se moveu"
        assert "(apertado)" in r["feito"]

    async def test_definir_valor_digita_no_campo_e_confirma(self, acoes):
        """Texto é digitado, nunca posto direto: foco pela acessibilidade,
        tudo selecionado, uma tecla por caractere. O mouse fica onde está."""
        _aprovar("s1")
        acoes.lista = [replace(USUARIO, valor="antigo"), *acoes.lista[1:]]
        r = await _lido_e({"acao": "definir_valor", "elemento": 1, "valor": "ana"}, _state())
        assert r["veredito"] == "confirmado" and acoes.acoes == [("focar_elemento", "Usuário")]
        teclas = [g for g in acoes.gestos if g[0] in ("tecla", "escrever")]
        assert teclas[:4] == [("tecla", "ctrl", True), ("tecla", "a", True), ("tecla", "a", False),
                              ("tecla", "ctrl", False)], "seleciona o que havia antes de digitar"
        assert [g for g in teclas if g[0] == "escrever"] == [("escrever", c) for c in "ana"]
        assert not [g for g in acoes.gestos if g[0] in ("mover", "botao")], "o mouse não se moveu"

    async def test_definir_valor_na_lista_escolhe_a_opcao(self, acoes):
        _aprovar("s1")
        acoes.lista = [*acoes.lista, Elemento("lista", "Estado", 900, 900, 300, 30, valor="SP")]
        r = await _lido_e({"acao": "definir_valor", "elemento": 4, "valor": "RJ"}, _state())
        assert r["veredito"] == "confirmado" and acoes.acoes == [("escolher_opcao", "Estado", "RJ")]
        assert digitado(acoes.gestos) == ""

    async def test_definir_valor_para_se_o_foco_cai_numa_senha(self, acoes, monkeypatch):
        _aprovar("s1")
        monkeypatch.setattr(acoes, "focar_elemento", lambda alvo: setattr(acoes, "em_foco", SENHA))
        r = await _lido_e({"acao": "definir_valor", "elemento": 1, "valor": "ana"}, _state())
        assert not r["ok"] and "senha" in r["erro"] and digitado(acoes.gestos) == ""

    async def test_definir_valor_em_campo_de_senha_e_recusado(self, acoes):
        _aprovar("s1")
        r = await _lido_e({"acao": "definir_valor", "elemento": 2, "valor": "hunter2"}, _state())
        assert r["campo_de_senha"] and acoes.acoes == []

    async def test_definir_valor_com_e_mail_pede_confirmacao(self, acoes):
        from tato.permissoes import limpar_sessao

        limpar_sessao("s1")
        r = await _lido_e({"acao": "definir_valor", "elemento": 1, "valor": "ana@exemplo.com"}, _state())
        assert r["chave"] == "computador_sensivel" and acoes.acoes == []

    async def test_sem_numero_nao_age(self, acoes):
        r = await _no_turno(computador.executar({"acao": "invocar"}, _state()))
        assert "elemento" in r["erro"] and acoes.acoes == []

    async def test_invocar_sem_efeito_sobe_o_degrau(self, acoes):
        _aprovar("s1")
        r = await _lido_e({"acao": "invocar", "elemento": 4}, _state())
        assert r["veredito"] == "sem_efeito_aparente" and "clicar" in r["proximo"]


class TestVeredito:
    def test_digitar_confirmado_pelo_valor_do_campo(self):
        from tato.computador.computador_veredito import veredito

        antes = [USUARIO]
        agora = [replace(USUARIO, valor="ana")]
        assert veredito("digitar", {"texto": "ana"}, USUARIO, antes, agora)["veredito"] == "confirmado"

    def test_mudou_traz_o_que_trocou_de_valor(self):
        from tato.computador.computador_veredito import veredito

        r = veredito("clicar", {}, None, [USUARIO, ENTRAR], [replace(USUARIO, valor="x"), ENTRAR])
        assert r["veredito"] == "mudou" and r["mudou"]["trocaram"] == [{"nome": "Usuário", "valor": ["", "x"]}]

    def test_sem_leitura_nao_da_para_confirmar(self):
        from tato.computador.computador_veredito import veredito

        assert veredito("clicar", {}, None, None, None)["veredito"] == "nao_da_para_confirmar"


class TestPrintQueSobra:
    async def test_o_print_velho_sai_depois_da_conferencia_por_texto(self, acoes):
        _aprovar("s1")
        state = _state()

        async def turno():
            await computador.executar({"acao": "ver", "marcar": True}, state)
            assert state.print_da_tela is not None
            return await computador.executar({"acao": "invocar", "elemento": 3}, state)

        r = await _no_turno(turno())
        assert state.print_da_tela is None and "saiu" in r["print_anterior"]

    async def test_ver_de_novo_sem_mudanca_avisa_tela_igual(self, acoes):
        state = _state()

        async def turno():
            await computador.executar({"acao": "ver"}, state)
            return await computador.executar({"acao": "ver"}, state)

        assert "idêntica" in (await _no_turno(turno()))["tela_igual"]

    async def test_print_igual_depois_do_gesto_e_sem_efeito(self, acoes, monkeypatch):
        def sem(*_):
            raise ErroDoComputador("sem AT-SPI")

        monkeypatch.setattr(acoes, "elementos", sem)
        _aprovar("s1")
        state = _state()

        async def turno():
            await computador.executar({"acao": "ver"}, state)
            return await computador.executar({"acao": "clicar", "x": 10, "y": 10}, state)

        r = await _no_turno(turno())
        assert r["veredito"] == "sem_efeito_aparente" and r["tela_igual"]


class TestArvoreQueAcorda:
    """O Chromium (Chrome, VS Code, Electron) liga a acessibilidade na primeira
    consulta: a primeira leitura vem só com os botões da moldura da janela."""

    class Acorda(ComElementos):
        def __init__(self):
            super().__init__()
            self.leituras = 0

        def elementos(self):
            self.leituras += 1
            moldura = [Elemento("botao", n, 1800 + i * 50, 0, 40, 40, janela="Editor") for i, n in
                       enumerate(("Minimizar", "Restaurar", "Fechar"))]
            if self.leituras == 1:
                return moldura
            return moldura + [Elemento("campo", "Mensagem", 900, 600, 700, 60, janela="Editor"),
                              Elemento("botao", "Enviar", 1700, 600, 100, 60, janela="Editor")]

    async def test_a_janela_magra_e_lida_de_novo_uma_vez(self, monkeypatch):
        monkeypatch.setattr(computador, "_ESPERA_DA_ARVORE_S", 0)
        backend = self.Acorda()
        computador.usar_backend(backend)
        try:
            r = await _no_turno(computador.executar({"acao": "elementos"}, _state()))
            assert backend.leituras == 2 and any(e["nome"] == "Mensagem" for e in r["elementos"])
            await _no_turno(computador.executar({"acao": "elementos"}, _state()))
            assert backend.leituras == 3, "a janela já acordou: não espera de novo"
        finally:
            computador._limpar_para_testes()
            computador.usar_backend(None)


class TestListaEnxuta:
    def test_item_e_link_no_mesmo_lugar_fica_o_link(self):
        item = Elemento("item", "Issues", 100, 180, 60, 20, janela="GitHub")
        link = Elemento("link", "Issues", 102, 178, 58, 22, janela="GitHub")
        outro = Elemento("link", "Issues", 900, 600, 58, 22, janela="GitHub")
        assert [(e.papel, e.x) for e in filtrar([item, link, outro], (1920, 1080))] == [("link", 102), ("link", 900)]

    def test_as_abas_de_fundo_saem_antes_do_conteudo(self):
        abas = [Elemento("aba", f"Aba {i}", 10 + i * 40, 5, 38, 30, janela="Chrome",
                         estado="selecionada" if i == 3 else "") for i in range(40)]
        pagina = [Elemento("link", f"Resultado {i}", 100, 200 + i * 14, 300, 12, janela="Chrome") for i in range(60)]
        lista = filtrar(abas + pagina, (1920, 1080))
        assert sum(e.papel == "aba" for e in lista) == 1 and lista[0].nome == "Aba 3"
        assert [e.nome for e in lista if e.papel == "link"] == [f"Resultado {i}" for i in range(60)]


class TestVereditoSemFalsoNegativo:
    """O gesto fez efeito, mas não na lista de nomes: desativar, rolar."""

    def test_campo_que_so_ficou_desativado_conta_como_mudanca(self):
        from tato.computador.computador_veredito import o_que_mudou

        antes = [Elemento("botao", "Procurar...", 10, 10, 50, 20)]
        agora = [Elemento("botao", "Procurar...", 10, 10, 50, 20, ativo=False)]
        mudou = o_que_mudou(antes, agora)["mudou"]
        assert mudou["trocaram"] == [{"nome": "Procurar...", "desativado": [False, True]}]

    async def test_rolagem_que_so_mexe_na_imagem_nao_e_sem_efeito(self, backend, monkeypatch):
        _aprovar("s1")
        imagens = iter([b"\x00" * 100, b"\xff" * 100])
        monkeypatch.setattr(computador, "_miniatura", lambda b, caixa: next(imagens, b"\xff" * 100))

        async def turno():
            await computador.executar({"acao": "elementos"}, _state())
            return await computador.executar({"acao": "rolar", "x": 500, "y": 400, "direcao": "baixo",
                                              "quantidade": 5}, _state())

        r = await _no_turno(turno())
        assert r["veredito"] == "mudou" and r["mudou_na_imagem"] is True
