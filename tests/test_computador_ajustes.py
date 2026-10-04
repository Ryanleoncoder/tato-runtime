from types import SimpleNamespace

import pytest

from tato.computador import computador
from tato.computador.computador_base import ErroDoComputador
from tato.computador.computador_elementos import Elemento, filtrar
from tato.computador.computador_moldura import Quadro
from test_computador import Falso, _aprovar, _no_turno, _state


class JanelaAlvo(Falso):
    def __init__(self):
        super().__init__()
        self.atual = "Página alvo"
        self.falha = False

    def janelas(self):
        return [{"titulo": self.atual, "em_foco": True, "x": 0, "y": 0,
                 "largura": 2560, "altura": 1440}]

    def focar(self, titulo):
        if self.falha:
            raise ErroDoComputador("janela ausente")
        self.gestos.append(("focar", titulo))
        self.atual = titulo
        return titulo


@pytest.mark.parametrize("falha", [False, True])
async def test_rolar_retoma_a_janela_lida_antes_do_gesto(monkeypatch, falha):
    backend = JanelaAlvo()
    computador.usar_backend(backend)
    monkeypatch.setattr(computador, "_ESPERA_DEPOIS_DO_GESTO", 0)
    _aprovar("s1")
    try:
        async def turno():
            await computador.executar({"acao": "janelas"}, _state())
            backend.atual, backend.falha = "IDE", falha
            return await computador.executar({"acao": "rolar", "x": 100, "y": 100}, _state())

        resultado = await _no_turno(turno())
        if falha:
            assert not resultado["executado"] and not backend.gestos
        else:
            assert resultado["executado"]
            assert backend.gestos[:3] == [("focar", "Página alvo"), ("mover", 176, 176), ("roda", 0, 3)]
            assert any(q.etiqueta and q.ponto is None for q in backend.quadros)
    finally:
        computador._limpar_para_testes()
        computador.usar_backend(None)


@pytest.mark.parametrize("editavel", [False, True])
def test_combobox_digita_so_quando_editavel(editavel):
    backend = Falso()
    gestos = []
    alvo = Elemento("lista", "Busca", 0, 0, 200, 30)
    backend.lista_editavel = lambda e: editavel
    backend.escolher_opcao = lambda e, v: gestos.append(("opcao", v))
    backend.focar_elemento = lambda e: gestos.append(("foco", e.nome))
    computador._executar_gesto(backend, "definir_valor", {"_alvo": alvo, "valor": "abc"}, (2560, 1440))
    assert gestos == ([("foco", "Busca")] if editavel else [("opcao", "abc")])
    assert "".join(g[1] for g in backend.gestos if g[0] == "escrever") == ("abc" if editavel else "")


def test_uia_identifica_edicao_sem_escrever(monkeypatch):
    from tato.computador import computador_uia as uia
    monkeypatch.setattr(uia, "_uia", lambda: object())
    monkeypatch.setattr(uia, "_achar", lambda auto, alvo: object())
    for padrao, esperado in [(None, False), (SimpleNamespace(CurrentIsReadOnly=True), False),
                             (SimpleNamespace(CurrentIsReadOnly=False), True)]:
        monkeypatch.setattr(uia, "_padrao", lambda *args: padrao)
        assert uia.lista_editavel(None) is esperado


def test_rolar_sem_coordenada_tira_o_mouse_da_janela_alheia():
    backend = JanelaAlvo()
    backend.atual = "IDE"
    backend.posicao_do_mouse = lambda: (3000, 1500)
    computador._executar_gesto(backend, "rolar", {"janela": "Página alvo"}, (2560, 1440))
    assert backend.gestos == [("focar", "Página alvo"), ("mover", 1280, 720), ("roda", 0, 3)]


def test_rolar_recusa_coordenada_fora_da_janela():
    backend = JanelaAlvo()
    backend.janelas = lambda: [{"titulo": "Página alvo", "em_foco": True,
                               "x": 0, "y": 0, "largura": 1000, "altura": 800}]
    with pytest.raises(ErroDoComputador, match="fora da janela"):
        computador._executar_gesto(backend, "rolar", {"janela": "Página alvo", "x": 800, "y": 100},
                                  (2560, 1440))
    assert backend.gestos == []


def test_atspi_identifica_combobox_editavel(monkeypatch):
    from tato.computador import computador_atspi as atspi
    estado = SimpleNamespace(EDITABLE=object())
    modulo = SimpleNamespace(StateType=estado)
    no = SimpleNamespace(get_state_set=lambda: SimpleNamespace(contains=lambda item: item is estado.EDITABLE))
    monkeypatch.setattr(atspi, "_atspi", lambda: modulo)
    monkeypatch.setattr(atspi, "_achar", lambda *args: no)
    assert atspi.lista_editavel(None)


@pytest.mark.parametrize("fixo", [False, True])
def test_badge_x11_segue_mouse_sem_trocar_de_canto(fixo):
    from tato.computador.computador_x11 import _Palco
    from tato.computador.computador_desenho import onde_esta_o_mouse
    quadro = Quadro(True, "Tato", (), False, etiqueta="Rolando", ponto=(400, 400) if fixo else None)
    redesenhos = []
    palco = SimpleNamespace(
        _d=SimpleNamespace(screen=lambda: SimpleNamespace(width_in_pixels=1000, height_in_pixels=800)),
        _quadro=quadro, _mouse=lambda: (550, 450), _mouse_desenhado=(500, 450),
        _perto=onde_esta_o_mouse((500, 450), (1000, 800), quadro),
        _redesenhar=lambda: redesenhos.append(True))
    _Palco._olhar_o_mouse(palco)
    assert redesenhos == ([] if fixo else [True])


def test_elementos_leem_a_sidebar_inteira_antes_do_conteudo():
    elementos = [Elemento("link", "conteúdo 1", 400, 100, 200, 30),
                 Elemento("link", "lateral 1", 10, 90, 130, 30),
                 Elemento("link", "conteúdo 2", 400, 180, 200, 30),
                 Elemento("link", "lateral 2", 10, 200, 130, 30)]
    assert [e.nome for e in filtrar(elementos, (1000, 800))] == [
        "lateral 1", "lateral 2", "conteúdo 1", "conteúdo 2"]


@pytest.mark.parametrize("fixo", [False, True])
def test_badge_windows_segue_mouse_sem_trocar_de_canto(fixo):
    from tato.computador.computador_windows import _PalcoWindows
    from tato.computador.computador_desenho import onde_esta_o_mouse
    palco = object.__new__(_PalcoWindows)
    palco.u = SimpleNamespace(GetSystemMetrics=lambda i: (1000, 800)[i])
    palco._quadro = Quadro(True, "Sentury", (), False,
                          etiqueta="Rolando", ponto=(400, 400) if fixo else None)
    palco._rodape = lambda: 0
    palco._mouse = lambda: (550, 450)
    palco._mouse_desenhado = (500, 450)
    palco._perto = onde_esta_o_mouse((500, 450), (1000, 800), palco._quadro, 0)
    redesenhos = []
    palco._aplicar = lambda: redesenhos.append(True)
    palco._olhar_o_mouse()
    assert redesenhos == ([] if fixo else [True])
