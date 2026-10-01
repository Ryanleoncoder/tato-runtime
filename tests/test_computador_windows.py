"""O backend do Windows fora do Windows: só o que não chama a API.

A janela, o `SendInput` e o `RegisterHotKey` precisam de uma máquina Windows
para serem vistos; aqui fica a aritmética que eles usam.
"""
import ctypes
from types import SimpleNamespace

import pytest

from tato.computador import computador_windows as w
from tato.computador.computador_base import ErroDoComputador


def test_bgra_premultiplicado():
    """O UpdateLayeredWindow quer BGRA com a cor ja multiplicada pelo alfa:
    sem isso o translucido sai claro demais."""
    from PIL import Image

    imagem = Image.new("RGBA", (2, 1))
    imagem.putpixel((0, 0), (200, 100, 50, 255))
    imagem.putpixel((1, 0), (200, 100, 50, 128))
    dados = w.bgra_premultiplicado(imagem)
    assert dados[:4] == bytes([50, 100, 200, 255])
    assert dados[4:] == bytes([round(50 * 128 / 255), round(100 * 128 / 255), round(200 * 128 / 255), 128])


def test_estruturas_da_janela_em_camadas():
    assert ctypes.sizeof(w._BITMAPINFOHEADER) == 40
    assert ctypes.sizeof(w._BLENDFUNCTION) == 4
    assert ctypes.sizeof(w._SIZE) == 8 and ctypes.sizeof(w._PONTO) == 8


def test_emoji_vai_em_par_substituto():
    assert w.unidades_utf16("a") == [0x61]
    assert w.unidades_utf16("ç") == [0xE7]
    assert w.unidades_utf16("😀") == [0xD83D, 0xDE00]


def test_atalho_de_parar():
    mascara, vk = w.modificadores_do_atalho("ctrl+alt+shift+s")
    assert mascara == w.MOD_CONTROL | w.MOD_ALT | w.MOD_SHIFT | w.MOD_NOREPEAT
    assert vk == ord("S")


@pytest.mark.parametrize("tecla,vk", [("enter", 0x0D), ("f5", 0x74), ("f24", 0x87), ("a", 0x41), ("7", 0x37)])
def test_vk(tecla, vk):
    assert w.vk_de(tecla) == vk


def test_tecla_desconhecida():
    with pytest.raises(ErroDoComputador):
        w.vk_de("ç")


def test_normalizar_vai_de_0_a_65535():
    assert w.normalizar(0, 1920) == 0
    assert w.normalizar(1919, 1920) == 65535
    assert w.normalizar(5000, 1920) == 65535  # fora da tela fica na borda


def test_estrutura_input_tem_o_tamanho_do_windows():
    """40 bytes em 64 bits: o SendInput recusa tamanho errado sem dizer por quê."""
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(w._INPUT) == 40


def test_fora_do_windows_diz_por_que():
    import sys

    if sys.platform.startswith("win"):
        pytest.skip("no Windows o backend abre de verdade")
    with pytest.raises(ErroDoComputador, match="só roda no Windows"):
        w.BackendWindows()


def test_bgra_reto_para_o_cursor():
    """O bitmap de cor de um cursor de 32 bits NAO e pre-multiplicado."""
    from PIL import Image

    imagem = Image.new("RGBA", (1, 1), (200, 100, 50, 128))
    assert w.bgra_reto(imagem) == bytes([50, 100, 200, 128])


def test_iconinfo_tem_o_tamanho_do_windows():
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(w._ICONINFO) == 32


def test_troca_o_de_texto_e_o_de_link_tambem():
    """Sem eles, em cima de um campo ou de um link o cursor voltava a ser o da pessoa."""
    assert set(w.CURSORES_TROCADOS) == {w.OCR_NORMAL, w.OCR_IBEAM, w.OCR_HAND}


class TestGravando:
    """Com `TATO_GRAVAR=1` a moldura não sai da captura, nem pisca em volta dela."""

    def _backend(self, pedidos):
        palco = SimpleNamespace(visivel=True, fora_da_captura=False, pedir=pedidos.append, sincronizar=lambda: None)
        backend = object.__new__(w.BackendWindows)
        backend._palco = palco
        return backend

    def test_sem_gravar_esconde_em_volta_do_print(self, monkeypatch):
        from PIL import ImageGrab

        monkeypatch.delenv("TATO_GRAVAR", raising=False)
        monkeypatch.setattr(ImageGrab, "grab", lambda bbox=None: "print")
        pedidos = []
        assert self._backend(pedidos)._capturar() == "print"
        assert pedidos == ["ocultar", "reexibir"]

    def test_gravando_a_moldura_fica(self, monkeypatch):
        from PIL import ImageGrab

        monkeypatch.setenv("TATO_GRAVAR", "1")
        monkeypatch.setattr(ImageGrab, "grab", lambda bbox=None: "print")
        pedidos = []
        assert self._backend(pedidos)._capturar() == "print"
        assert pedidos == [] and w.gravando()


def test_fechar_moldura_espera_devolver_cursor(monkeypatch):
    eventos = []
    palco = SimpleNamespace(pedir=lambda *args: eventos.append(args),
                            sincronizar=lambda: eventos.append(("sincronizado",)))
    monkeypatch.setattr(w.BackendWindows, "_palco_vivo", lambda self: palco)
    backend = object.__new__(w.BackendWindows)
    backend.desenhar(SimpleNamespace(aberta=True))
    assert len(eventos) == 1
    backend.desenhar(SimpleNamespace(aberta=False))
    assert eventos[-1] == ("sincronizado",)


def test_restauracao_falha_preserva_marcador_para_proxima_tentativa(monkeypatch, tmp_path):
    marcador = tmp_path / "cursor"
    marcador.write_text("1")
    monkeypatch.setattr(w, "_marca_do_cursor", lambda: marcador)
    recusou = SimpleNamespace(SystemParametersInfoW=lambda *args: 0)
    assert w.devolver_cursor(recusou) is False
    assert marcador.exists()
    aceitou = SimpleNamespace(SystemParametersInfoW=lambda *args: 1)
    assert w.devolver_cursor(aceitou) is True
    assert not marcador.exists()


@pytest.mark.parametrize("varredura,caps,c,esperado", [
    (0x0041, False, "a", (0x41, False)),     # a: a tecla A
    (0x0141, False, "A", (0x41, True)),      # A: shift + A
    (0x0132, False, "@", (0x32, True)),      # @ no ABNT2: shift + 2
    (0x0641, False, "€", None),              # AltGr (ctrl+alt): vai por unicode
    (-1, False, "á", None),                  # sem tecla (acento de tecla morta)
    (0x0041, True, "a", None),               # CapsLock ligado: o shift sairia invertido
    (0x0031, True, "1", (0x31, False)),      # CapsLock não mexe em dígito
])
def test_texto_pela_tecla_do_layout(varredura, caps, c, esperado):
    assert w.pela_tecla(varredura, caps, c) == esperado
