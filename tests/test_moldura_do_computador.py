"""A moldura do computer use: selo, as tres ultimas falas e o atalho que para
de verdade.

O backend do Windows (a janela em camadas) ainda nao existe; aqui roda o
falso, que guarda o que seria desenhado.
"""
import asyncio
import threading

import pytest

from tato.computador.computador_moldura import ATALHO_PADRAO, BackendFalso, Moldura, MolduraParada, atalho_de_parar, normalizar_atalho, texto_do_selo


class TestAtalho:
    @pytest.mark.parametrize("texto,esperado", [
        ("Ctrl + Shift + Alt + S", "ctrl+alt+shift+s"),
        ("control+option+q", "ctrl+alt+q"),
        ("cmd+shift+F12", "shift+win+f12"),
    ])
    def test_normaliza(self, texto, esperado):
        assert normalizar_atalho(texto) == esperado

    @pytest.mark.parametrize("texto", ["s", "ctrl+s", "ctrl+alt", "ctrl+alt+s+x", "ctrl+ctrl+s", ""])
    def test_recusa_o_que_colide_ou_se_aperta_sem_querer(self, texto):
        with pytest.raises(ValueError):
            normalizar_atalho(texto)

    def test_padrao_e_valido(self):
        assert normalizar_atalho(ATALHO_PADRAO) == ATALHO_PADRAO

    def test_do_ambiente(self, monkeypatch):
        monkeypatch.setenv("TATO_COMPUTADOR_ATALHO", "Ctrl+Alt+Pause")
        assert atalho_de_parar() == "ctrl+alt+pause"

    def test_invalido_no_ambiente_nao_desliga_o_parar(self, monkeypatch):
        monkeypatch.setenv("TATO_COMPUTADOR_ATALHO", "esc")
        assert atalho_de_parar() == ATALHO_PADRAO

    def test_selo(self):
        assert texto_do_selo("ctrl+alt+shift+s") == \
            "Tato está usando o computador · para parar, pressione Ctrl + Alt + Shift + S"


def _moldura(**kw):
    backend = BackendFalso()
    paradas = []
    m = Moldura(backend, ao_parar=lambda: paradas.append("turno"), atalho="ctrl+alt+shift+s", **kw)
    return m, backend, paradas


class TestFalas:
    def test_tres_ultimas_esmaecendo_e_a_quarta_some(self):
        m, backend, _ = _moldura()
        with m:
            for texto in ("abrindo o jogo", "indo ao menu", "escolhendo o pokemon", "confirmando"):
                m.falar(texto)
            falas = backend.ultimo.falas
        assert [f.texto for f in falas] == ["indo ao menu", "escolhendo o pokemon", "confirmando"]
        assert [f.opacidade for f in falas] == [0.3, 0.6, 1.0]

    def test_a_primeira_fala_e_inteira(self):
        m, backend, _ = _moldura()
        with m:
            m.falar("abrindo o jogo")
            assert [(f.texto, f.opacidade) for f in backend.ultimo.falas] == [("abrindo o jogo", 1.0)]

    def test_repetida_nao_empurra_as_outras(self):
        m, backend, _ = _moldura()
        with m:
            m.falar("a")
            m.falar("b")
            m.falar("b")
            assert [f.texto for f in backend.ultimo.falas] == ["a", "b"]

    def test_uma_linha_e_curta(self):
        m, backend, _ = _moldura()
        with m:
            m.falar("linha\n\num   dois " + "x" * 300)
            texto = backend.ultimo.falas[-1].texto
        assert "\n" not in texto and texto.startswith("linha um dois") and len(texto) == 120

    def test_ouve_a_fala_do_tool_start(self):
        m, backend, _ = _moldura()
        with m:
            m.ouvir({"type": "tool_start", "tool": "clicar", "fala": "clicando em Continuar"})
            m.ouvir({"type": "stage", "text": "Pensando…"})
            m.ouvir({"type": "tool_start", "tool": "clicar"})
            assert [f.texto for f in backend.ultimo.falas] == ["clicando em Continuar"]

    def test_fechada_nao_fala(self):
        m, backend, _ = _moldura()
        m.falar("antes de abrir")
        assert backend.quadros == []


class TestAbrirEFechar:
    def test_abre_com_selo_e_ouvindo_o_atalho(self):
        m, backend, _ = _moldura(reduzir_movimento=True)
        m.abrir()
        assert backend.atalho == "ctrl+alt+shift+s"
        assert backend.ultimo.aberta and "Ctrl + Alt + Shift + S" in backend.ultimo.selo
        assert backend.ultimo.reduzir_movimento is True

    def test_fechar_para_de_ouvir_e_apaga(self):
        m, backend, paradas = _moldura()
        with m:
            m.falar("x")
        assert backend.atalho is None
        assert not backend.ultimo.aberta and backend.ultimo.selo == "" and backend.ultimo.falas == ()
        assert paradas == [] and backend.soltou == 0  # fechar nao e parar


class TestParar:
    def test_o_atalho_solta_para_o_turno_e_fecha(self):
        m, backend, paradas = _moldura()
        m.abrir()
        backend.apertar()
        assert backend.soltou == 1 and paradas == ["turno"]
        assert not backend.ultimo.aberta and backend.atalho is None

    def test_nenhum_gesto_depois(self):
        m, backend, _ = _moldura()
        m.abrir()
        m.conferir()
        backend.apertar()
        with pytest.raises(MolduraParada):
            m.conferir()

    def test_nao_reabre_nem_fala_depois(self):
        m, backend, _ = _moldura()
        m.abrir()
        backend.apertar()
        m.falar("tarde demais")
        assert not backend.ultimo.aberta
        with pytest.raises(MolduraParada):
            m.abrir()

    def test_duas_vezes_e_uma(self):
        m, backend, paradas = _moldura()
        m.abrir()
        m.parar()
        m.parar()
        assert paradas == ["turno"] and backend.soltou == 1

    def test_etapa_que_falha_nao_impede_as_outras(self):
        backend = BackendFalso()

        def quebra():
            raise RuntimeError("soltar falhou")

        backend.soltar_entrada = quebra
        paradas = []
        m = Moldura(backend, ao_parar=lambda: paradas.append("turno"), atalho="ctrl+alt+shift+s")
        m.abrir()
        backend.apertar()
        assert paradas == ["turno"] and not m.aberta
        with pytest.raises(MolduraParada):
            m.conferir()

    def test_de_outra_thread(self):
        """O atalho e ouvido pelo sistema, fora do loop."""
        m, backend, paradas = _moldura()
        m.abrir()
        t = threading.Thread(target=backend.apertar)
        t.start()
        t.join(2)
        assert paradas == ["turno"] and m.parada
