"""Conferências antes e depois do gesto: alvo que mudou desde o print, texto
que o navegador ainda não montou, pausa de Tab e Enter no ritmo rápido."""
import asyncio
from types import SimpleNamespace

from PIL import Image, ImageDraw

from tato.computador import computador
from tato.computador.computador_base import tela_de_imagem
from tests.test_computador import Falso


class ComImagem(Falso):
    """Tela de verdade em 1920x1080: o print e o recorte saem da imagem atual."""

    def __init__(self):
        super().__init__((1920, 1080))
        self.imagem = Image.new("RGB", (1920, 1080), (240, 240, 240))

    def tela(self):
        return tela_de_imagem(self.imagem)


def _turno(backend, gestos_depois=1):
    tela = backend.tela()
    return SimpleNamespace(extras={"print_ref": (tela.png, tela.largura_real, tela.altura_real),
                                   "gestos_desde_o_print": gestos_depois})


def _cobrir(backend, caixa, cor=(30, 30, 160)):
    ImageDraw.Draw(backend.imagem).rectangle(caixa, fill=cor)


class TestAlvoMudou:
    def _alvo(self, backend, turno, **extra):
        # (700, 400) no print de 1456 é (923, 527) na tela de 1920.
        argumentos = {"x": 700, "y": 400, **extra}
        return asyncio.run(computador._alvo_mudou(backend, turno, "clicar", argumentos, (1920, 1080)))

    def test_tela_igual_ao_print_deixa_clicar(self):
        backend = ComImagem()
        assert self._alvo(backend, _turno(backend)) is None

    def test_dialogo_que_mudou_em_volta_do_alvo_recusa(self):
        backend = ComImagem()
        turno = _turno(backend)
        _cobrir(backend, (850, 450, 1000, 600))
        recusa = self._alvo(backend, turno)
        assert recusa["recusado"] and not recusa["executado"] and "confiar_no_print" in recusa["proximo"]

    def test_fundo_cinza_que_virou_branco_conta(self):
        backend = ComImagem()
        _cobrir(backend, (700, 300, 1200, 800), cor=(235, 235, 235))
        turno = _turno(backend)
        _cobrir(backend, (700, 300, 1200, 800), cor=(255, 255, 255))
        assert self._alvo(backend, turno)["recusado"]

    def test_mudanca_longe_do_alvo_nao_conta(self):
        backend = ComImagem()
        turno = _turno(backend)
        _cobrir(backend, (100, 100, 300, 300))
        assert self._alvo(backend, turno) is None

    def test_sem_gesto_depois_do_print_nao_confere(self):
        backend = ComImagem()
        turno = _turno(backend, gestos_depois=0)
        _cobrir(backend, (850, 450, 1000, 600))
        assert self._alvo(backend, turno) is None

    def test_quem_conta_com_a_mudanca_passa(self):
        backend = ComImagem()
        turno = _turno(backend)
        _cobrir(backend, (850, 450, 1000, 600))
        assert self._alvo(backend, turno, confiar_no_print=True) is None
        assert self._alvo(backend, turno, elemento=3) is None


class TestLerDoNavegador:
    def _backend(self, frente, respostas):
        backend = Falso()
        backend.programa_da_frente = lambda: frente
        backend.texto = lambda maximo: respostas.pop(0)
        return backend

    def test_pagina_que_ainda_nao_montou_le_de_novo(self, monkeypatch):
        async def sem_espera(_s):
            return None

        monkeypatch.setattr(computador.asyncio, "sleep", sem_espera)
        respostas = ["lista.mercadolivre.com.br/fone", "Fone JBL | 199 reais " * 30]
        backend = self._backend("C:/Program Files/Google/chrome.exe", respostas)
        texto = asyncio.run(computador._ler_texto(backend, 10000, ""))
        assert "199 reais" in texto and respostas == []

    def test_fora_do_navegador_le_uma_vez(self):
        respostas = ["texto curto", "não lê de novo"]
        backend = self._backend("notepad.exe", respostas)
        assert asyncio.run(computador._ler_texto(backend, 10000, "")) == "texto curto"
        assert respostas == ["não lê de novo"]

    def test_preco_em_reais_vira_dica(self):
        assert "reais" in computador._sem_trechos("R$")
        assert "reais" not in computador._sem_trechos("Tune 520")


def test_ritmo_rapido_espera_mais_depois_de_tab_e_enter(monkeypatch):
    import time

    pausas = []
    monkeypatch.setattr(time, "sleep", pausas.append)
    Falso().digitar("a\tb\nc", ritmo="rapido")
    assert pausas == [0.05, 0.25, 0.05, 0.25]
