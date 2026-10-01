"""Testa borda, aviso, falas recentes, ponteiro e efeito de clique."""
import pytest

from tato.computador.computador import etiqueta_do_gesto
from tato.computador.computador_base import para_exibir
from tato.computador.computador_desenho import PALETA, com_cursor, desenhar, mouse_perto
from tato.computador.computador_moldura import BackendFalso, Fala, Moldura, Quadro

TELA = (1440, 900)


def _quadro(**kw):
    base = dict(aberta=True, selo="", reduzir_movimento=False, atalho="Ctrl+Alt+Shift+S",
                falas=(Fala("antiga", 0.3), Fala("do meio", 0.6), Fala("a de agora", 1.0)),
                contexto="Pokédex")
    base.update(kw)
    return Quadro(**base)


def _nomes(regioes):
    return {r.nome for r in regioes}


def test_fechada_nao_desenha_nada():
    camada, regioes = desenhar(TELA, _quadro(aberta=False), (10, 10))
    assert regioes == [] and camada.getbbox() is None


def test_as_partes_do_artefato():
    camada, regioes = desenhar(TELA, _quadro(etiqueta="clicar", clique=True), (655, 301))
    assert {"pilula", "caixinha", "balao", "onda", "borda_cima"} <= _nomes(regioes)
    assert camada.getpixel((1, 450))[3] == 255
    pilula = next(r for r in regioes if r.nome == "pilula")
    assert abs((pilula.x + pilula.largura / 2) - TELA[0] / 2) <= 1 and pilula.y == 10  # no topo, no meio


def test_sem_gesto_nao_ha_balao_nem_onda():
    _, regioes = desenhar(TELA, _quadro(), (655, 301))
    assert not {"balao", "onda"} & _nomes(regioes)


def test_a_caixinha_troca_de_canto_quando_o_mouse_chega_perto():
    _, longe = desenhar(TELA, _quadro(), (200, 200))
    _, perto = desenhar(TELA, _quadro(), (1350, 850))
    caixa_longe = next(r for r in longe if r.nome == "caixinha")
    caixa_perto = next(r for r in perto if r.nome == "caixinha")
    assert caixa_longe.y > TELA[1] / 2 and caixa_perto.y == 52
    assert mouse_perto((1350, 850), TELA) and not mouse_perto((200, 200), TELA)


def test_a_mensagem_antiga_fica_mais_apagada_que_a_de_agora():
    """Alfa no texto substituia o da caixinha: a linha antiga ficava MAIS
    clara que a atual. Agora a cor mistura, e a antiga e mais escura."""
    camada, regioes = desenhar(TELA, _quadro(falas=(Fala("MMMMMMMM", 0.3), Fala("MMMMMMMM", 1.0))), None)
    caixa = next(r for r in regioes if r.nome == "caixinha")

    def mais_claro(y0, y1):
        return max(sum(camada.getpixel((x, y))[:3]) for x in range(caixa.x + 14, caixa.x + 100)
                   for y in range(y0, y1))

    topo = caixa.y + 12 + 16 + 8
    assert mais_claro(topo, topo + 16) < mais_claro(topo + 23, topo + 39)


def test_sem_brilho_a_borda_e_so_a_solida():
    _, regioes = desenhar(TELA, _quadro(), None, brilho=False)
    assert next(r for r in regioes if r.nome == "borda_cima").altura == 4


def test_balao_vira_perto_da_borda():
    _, regioes = desenhar(TELA, _quadro(etiqueta="clicar com o direito"), (1435, 895))
    balao = next(r for r in regioes if r.nome == "balao")
    assert balao.x + balao.largura <= TELA[0] and balao.y + balao.altura <= TELA[1]


def test_o_cursor_aparece_no_print():
    from PIL import Image

    tela = Image.new("RGB", (200, 200), (0, 0, 0))
    assert com_cursor(tela, None) is tela
    com = com_cursor(tela, (100, 100))
    assert com.getpixel((102, 104)) == (255, 255, 255)  # dentro da seta
    assert com.getpixel((150, 150)) == (0, 0, 0)


def test_a_moldura_leva_o_gesto_ao_balao():
    backend = BackendFalso()
    m = Moldura(backend, ao_parar=lambda: None, atalho="ctrl+alt+shift+s", contexto="Pokédex")
    m.gesto("clicar", True)
    assert backend.quadros == []  # fechada, nada
    m.abrir()
    m.gesto("clicar", True)
    assert backend.ultimo.etiqueta == "clicar" and backend.ultimo.clique
    assert backend.ultimo.atalho == "Ctrl+Alt+Shift+S" and backend.ultimo.contexto == "Pokédex"
    m.parar()
    m.gesto("tarde", False)
    assert backend.ultimo.etiqueta == ""


def test_nomes_de_tecla_para_a_tela():
    assert para_exibir("cima") == "↑" and para_exibir("ctrl+shift+t") == "Ctrl+Shift+T"
    assert etiqueta_do_gesto("tecla", {"tecla": "enter"}) == "tecla · Enter"
    assert etiqueta_do_gesto("digitar", {"texto": "senha123"}) == "digitar · 8 caracteres"
    assert "senha" not in etiqueta_do_gesto("digitar", {"texto": "senha123"})


class TestUmVisualSo:
    """Qualquer agente: o degradê do ciano ao coral e a mesma seta."""

    @pytest.mark.parametrize("agente", ["Tato", "Claude Code"])
    def test_a_borda_vai_do_ciano_ao_coral(self, agente):
        camada, _ = desenhar(TELA, _quadro(agente=agente), (655, 301))
        esquerda, direita = camada.getpixel((1, 450))[:3], camada.getpixel((TELA[0] - 2, 450))[:3]
        assert sum(abs(a - b) for a, b in zip(esquerda, PALETA[0])) < 40
        assert sum(abs(a - b) for a, b in zip(direita, PALETA[-1])) < 40

    def test_a_seta(self):
        """Ciano à esquerda, magenta à direita, sem amarelo."""
        from tato.computador.computador_desenho import seta

        desenho, (qx, qy) = seta(32)
        assert desenho.size == (32, 32) and (qx, qy) == (5, 4)
        amarelos = sum(1 for p in desenho.getdata() if p[3] > 200 and p[0] > 200 and p[1] > 150 and p[2] < 80)
        assert amarelos == 0
        esquerda, direita = desenho.getpixel((8, 10)), desenho.getpixel((26, 19))
        assert esquerda[3] > 200 and esquerda[1] > 120 and direita[3] > 200 and direita[0] > 100


def test_o_print_mostra_a_seta_com_a_moldura_aberta():
    from PIL import Image

    tela = Image.new("RGB", (200, 200), (255, 255, 255))
    com = com_cursor(tela, (100, 100), com_seta=True)
    r, g, b = com.getpixel((106, 106))  # dentro da seta, perto da ponta
    assert (r, g, b) != (255, 255, 255) and b > 150


def test_a_pilula_desce_quando_o_mouse_chega_perto():
    """Como a caixinha: o mouse no alto, no meio, e a pílula vai para baixo, acima da barra."""
    def pilula(mouse):
        _, regioes = desenhar(TELA, _quadro(), mouse, rodape=48)
        return next(r for r in regioes if r.nome == "pilula")

    longe, perto = pilula((100, 500)), pilula((TELA[0] // 2, 20))
    assert longe.y == 10
    assert perto.y + perto.altura <= TELA[1] - 48 and perto.y > TELA[1] // 2
