"""Opera o computador no X11 com XTEST e captura a tela com Pillow.

A moldura e o atalho usam uma conexão própria numa thread dedicada.
Essa separação evita compartilhar a conexão Xlib entre threads.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .computador_base import BackendDoComputador, ErroDoComputador, Tela, ler_combinacao, tela_de_imagem

logger = logging.getLogger(__name__)

_KEYSYM = {
    "ctrl": "Control_L", "alt": "Alt_L", "shift": "Shift_L", "win": "Super_L",
    "enter": "Return", "tab": "Tab", "esc": "Escape", "espaco": "space", "backspace": "BackSpace",
    "delete": "Delete", "insert": "Insert", "home": "Home", "end": "End", "pageup": "Prior",
    "pagedown": "Next", "cima": "Up", "baixo": "Down", "esquerda": "Left", "direita": "Right",
    "capslock": "Caps_Lock", "printscreen": "Print", "menu": "Menu",
    "=": "equal", "-": "minus", ",": "comma", ".": "period",
}
_BOTAO = {"esquerdo": 1, "meio": 2, "direito": 3}


def _keysym_de_nome(nome: str) -> int:
    from Xlib import XK

    if nome.startswith("f") and nome[1:].isdigit():
        simbolo = XK.string_to_keysym(nome.upper())
    else:
        simbolo = XK.string_to_keysym(_KEYSYM.get(nome, nome))
    if not simbolo:
        raise ErroDoComputador(f"tecla sem símbolo no X11: {nome!r}")
    return simbolo


def _keysym_de_caractere(c: str) -> int:
    if c == "\n":
        return _keysym_de_nome("enter")
    if c == "\t":
        return _keysym_de_nome("tab")
    codigo = ord(c)
    # Latin-1 tem keysym igual ao código; o resto é 0x01000000 + código.
    if 0x20 <= codigo <= 0x7E or 0xA0 <= codigo <= 0xFF:
        return codigo
    return 0x01000000 + codigo


class BackendX11(BackendDoComputador):
    nome = "x11"

    def __init__(self, display_nome: str) -> None:
        super().__init__()
        try:
            from Xlib import display
            from Xlib.ext import xtest  # noqa: F401
        except ImportError as exc:  # pragma: no cover - dependência de Linux
            raise ErroDoComputador("falta o python-xlib (pip install python-xlib)") from exc
        self._nome = display_nome
        try:
            self._d = display.Display(display_nome)
        except Exception as exc:
            raise ErroDoComputador(f"não consegui abrir a tela {display_nome}: {exc}") from exc
        if not self._d.has_extension("XTEST"):
            raise ErroDoComputador("o servidor X não tem XTEST: não há como mexer no mouse")
        self._raiz = self._d.screen().root
        self._trava = threading.RLock()
        self._palco: Optional[_Palco] = None

    # Leitura
    def tamanho(self) -> Tuple[int, int]:
        tela = self._d.screen()
        return tela.width_in_pixels, tela.height_in_pixels

    def tela(self) -> Tela:
        """Captura a tela sem a moldura, que é ocultada temporariamente."""
        from PIL import ImageGrab

        palco = self._palco if self._palco is not None and self._palco.visivel else None
        if palco is not None:
            palco.pedir("ocultar")
            palco.sincronizar()
        try:
            imagem = ImageGrab.grab(xdisplay=self._nome)
        except Exception as exc:
            raise ErroDoComputador(f"não consegui tirar o print: {exc}") from exc
        finally:
            if palco is not None:
                palco.pedir("reexibir")
        # O print do X não traz o cursor: sem ele, ninguém sabe onde o mouse
        # está. Com a moldura na tela, o cursor é a seta de quem usa.
        from .computador_desenho import com_cursor
        from .computador_moldura import agente

        return tela_de_imagem(com_cursor(imagem, self.posicao_do_mouse(), com_seta=palco is not None,
                                         agente=agente()))

    def posicao_do_mouse(self) -> Tuple[int, int]:
        with self._trava:
            ponteiro = self._raiz.query_pointer()
        return ponteiro.root_x, ponteiro.root_y

    def elementos(self) -> list:
        from .computador_atspi import elementos

        return elementos()

    def foco(self):
        from .computador_atspi import foco

        try:
            return foco()
        except ErroDoComputador:
            return None

    def invocar(self, alvo) -> str:
        from .computador_atspi import invocar

        return invocar(alvo)

    def focar_elemento(self, alvo) -> None:
        from .computador_atspi import focar

        focar(alvo)

    def escolher_opcao(self, alvo, opcao: str) -> None:
        from .computador_atspi import escolher_opcao

        escolher_opcao(alvo, opcao)

    def janelas(self) -> List[Dict[str, object]]:
        with self._trava:
            d = self._d
            lista_atom = d.intern_atom("_NET_CLIENT_LIST")
            ativa_atom = d.intern_atom("_NET_ACTIVE_WINDOW")
            nome_atom = d.intern_atom("_NET_WM_NAME")
            prop = self._raiz.get_full_property(lista_atom, 0)
            ativa = self._raiz.get_full_property(ativa_atom, 0)
            if ativa is not None and len(ativa.value):
                id_ativa = ativa.value[0]
            else:
                # Sem gerenciador de janelas, o foco de entrada do próprio X.
                foco = d.get_input_focus().focus
                id_ativa = getattr(foco, "id", None)
            if prop is not None:
                ids = list(prop.value)
                candidatas = [d.create_resource_object("window", i) for i in ids]
            else:
                # Sem gerenciador de janelas (Xvfb puro): as filhas da raiz.
                candidatas = self._raiz.query_tree().children
            saida = []
            for janela in candidatas:
                try:
                    atributos = janela.get_attributes()
                    if atributos.map_state != 2 or atributos.override_redirect:  # IsViewable
                        continue
                    titulo_prop = janela.get_full_property(nome_atom, 0)
                    titulo = (titulo_prop.value.decode("utf-8", "replace") if titulo_prop
                              else (janela.get_wm_name() or ""))
                    geo = janela.get_geometry()
                    pos = janela.translate_coords(self._raiz, 0, 0)
                    saida.append({"titulo": str(titulo), "em_foco": janela.id == id_ativa,
                                  "x": -pos.x, "y": -pos.y, "largura": geo.width, "altura": geo.height})
                except Exception:
                    continue
            return saida

    # Gestos
    def mover(self, x: int, y: int) -> None:
        from Xlib import X
        from Xlib.ext import xtest

        with self._trava:
            xtest.fake_input(self._d, X.MotionNotify, x=int(x), y=int(y))
            self._d.sync()

    def botao(self, botao: str, apertar: bool) -> None:
        from Xlib import X
        from Xlib.ext import xtest

        numero = _BOTAO.get(botao)
        if numero is None:
            raise ErroDoComputador(f"botão desconhecido: {botao!r}")
        with self._trava:
            xtest.fake_input(self._d, X.ButtonPress if apertar else X.ButtonRelease, numero)
            self._d.sync()
        (self._apertou if apertar else self._soltou)(f"botao:{botao}")

    def tecla_bruta(self, tecla: str, apertar: bool) -> None:
        with self._trava:
            codigo = self._d.keysym_to_keycode(_keysym_de_nome(tecla))
            if not codigo:
                raise ErroDoComputador(f"tecla sem código neste teclado: {tecla!r}")
            self._codigo(codigo, apertar)
        (self._apertou if apertar else self._soltou)(f"tecla:{tecla}")

    def _codigo(self, codigo: int, apertar: bool) -> None:
        from Xlib import X
        from Xlib.ext import xtest

        xtest.fake_input(self._d, X.KeyPress if apertar else X.KeyRelease, codigo)
        self._d.sync()

    def escrever(self, texto: str) -> None:
        with self._trava:
            shift = self._d.keysym_to_keycode(_keysym_de_nome("shift"))
            for c in str(texto):
                simbolo = _keysym_de_caractere(c)
                codigo, indice = next(((k, i) for k, i in self._d.keysym_to_keycodes(simbolo) if i in (0, 1)),
                                      (0, 0))
                if codigo:
                    if indice == 1:
                        self._codigo(shift, True)
                    self._codigo(codigo, True)
                    self._codigo(codigo, False)
                    if indice == 1:
                        self._codigo(shift, False)
                else:
                    self._por_codigo_emprestado(simbolo)

    def _por_codigo_emprestado(self, simbolo: int) -> None:
        """Caractere que o teclado não tem (acento num layout US, emoji):
        empresta um código livre, aperta, devolve. É o que o xdotool faz."""
        d = self._d
        minimo, maximo = d.display.info.min_keycode, d.display.info.max_keycode
        mapa = d.get_keyboard_mapping(minimo, maximo - minimo + 1)
        livre = next((minimo + i for i, simbolos in enumerate(mapa) if not any(simbolos)), None)
        if livre is None:
            raise ErroDoComputador("nenhum código de tecla livre para digitar esse caractere")
        largura = len(mapa[0]) if mapa else 1
        d.change_keyboard_mapping(livre, [(simbolo,) * largura])
        d.sync()
        try:
            time.sleep(0.02)  # o cliente precisa ver o mapa novo antes da tecla
            self._codigo(livre, True)
            self._codigo(livre, False)
            time.sleep(0.02)
        finally:
            d.change_keyboard_mapping(livre, [(0,) * largura])
            d.sync()

    def roda(self, dx: int, dy: int) -> None:
        for botao, vezes in ((5 if dy > 0 else 4, abs(dy)), (7 if dx > 0 else 6, abs(dx))):
            for _ in range(min(int(vezes), 50)):
                from Xlib import X
                from Xlib.ext import xtest

                with self._trava:
                    xtest.fake_input(self._d, X.ButtonPress, botao)
                    xtest.fake_input(self._d, X.ButtonRelease, botao)
                    self._d.sync()

    # A moldura e o atalho
    def _palco_vivo(self) -> "_Palco":
        if self._palco is None or not self._palco.is_alive():
            self._palco = _Palco(self._nome)
            self._palco.start()
            self._palco.pronto.wait(5)
        return self._palco

    def desenhar(self, quadro) -> None:
        self._palco_vivo().pedir("desenhar", quadro)

    def ouvir_atalho(self, atalho: str, ao_apertar: Callable[[], None]) -> None:
        """Sem o atalho registrado, recusa: agir sem um jeito de parar é o
        que a moldura existe para impedir."""
        resposta: "queue.Queue" = queue.Queue()
        self._palco_vivo().pedir("ouvir", atalho, ao_apertar, resposta)
        try:
            erro = resposta.get(timeout=5)
        except queue.Empty:
            erro = "sem resposta da thread da moldura"
        if erro:
            raise ErroDoComputador(f"o atalho de parar ({atalho}) não pôde ser registrado: {erro}")

    def parar_de_ouvir(self) -> None:
        if self._palco is not None:
            self._palco.pedir("parar_de_ouvir")

    def estado_da_moldura(self) -> Dict[str, object]:
        """Para o teste e o diagnóstico."""
        if self._palco is None:
            return {"visivel": False, "ouvindo": None, "seta": False, "cursor_escondido": False}
        self._palco.sincronizar()
        return {"visivel": self._palco.visivel, "ouvindo": self._palco.atalho,
                "seta": self._palco._seta is not None, "cursor_escondido": self._palco._cursor_escondido}

    def fechar(self) -> None:
        if self._palco is not None:
            self._palco.pedir("fim")
            self._palco.join(2)
            self._palco = None
        try:
            self._d.close()
        except Exception:
            pass


class _Palco(threading.Thread):
    """Desenha a moldura e recebe o atalho numa conexão X11 dedicada.

    Sem compositor, cada região combina seu desenho com a tela abaixo dela.
    A moldura é ocultada durante a leitura da tela para compor a próxima imagem.
    """

    _OLHAR_O_MOUSE = 0.15

    def __init__(self, display_nome: str) -> None:
        super().__init__(daemon=True, name="tato-moldura-x11")
        self._nome = display_nome
        self._pedidos: "queue.Queue" = queue.Queue()
        self.pronto = threading.Event()
        self.visivel = False
        self.atalho: Optional[str] = None
        self._ao_apertar: Optional[Callable[[], None]] = None
        self._quadro = None
        self._janelas: Dict[str, object] = {}
        self._imagens: Dict[str, object] = {}
        self._agarrados: list = []
        self._perto: Optional[tuple] = None
        # A seta do agente acompanha o mouse enquanto XFixes oculta o cursor original.
        self._seta = None
        self._imagem_da_seta = None
        self._quente = (0, 0)
        self._onde_a_seta_esta: Optional[Tuple[int, int]] = None
        self._cursor_escondido = False

    def pedir(self, *pedido) -> None:
        self._pedidos.put(pedido)

    def sincronizar(self, prazo: float = 2.0) -> None:
        """Espera a fila de pedidos esvaziar (teste e `ouvir_atalho`)."""
        feito = threading.Event()
        self.pedir("marca", feito)
        feito.wait(prazo)

    def run(self) -> None:
        from Xlib import X, display

        try:
            self._d = display.Display(self._nome)
        except Exception:
            logger.warning("Moldura X11 sem conexão com a tela.", exc_info=True)
            self.pronto.set()
            return
        self._raiz = self._d.screen().root
        self.pronto.set()
        vivo, olhou = True, time.monotonic()
        while vivo:
            while self._d.pending_events():
                evento = self._d.next_event()
                if evento.type == X.Expose:
                    if self._seta is not None and evento.window == self._seta:
                        self._pintar_a_seta()
                        continue
                    nome = next((n for n, j in self._janelas.items() if j == evento.window), None)
                    if nome:
                        self._pintar(nome)
                elif evento.type == X.KeyPress and self._ao_apertar is not None:
                    # Em outra thread: o parar fecha a moldura, que pede a
                    # esta thread; chamar daqui travaria na própria fila.
                    threading.Thread(target=self._ao_apertar, daemon=True).start()
            if self.visivel and time.monotonic() - olhou > self._OLHAR_O_MOUSE:
                olhou = time.monotonic()
                self._olhar_o_mouse()
            if self._seta is not None:
                self._seguir_o_mouse()
            try:
                pedido = self._pedidos.get(timeout=0.015)
            except queue.Empty:
                continue
            try:
                vivo = self._atender(pedido)
            except Exception:
                logger.warning("Pedido da moldura X11 falhou: %s", pedido[0], exc_info=True)
        self._soltar_atalho()
        self._esconder()
        self._d.close()

    def _atender(self, pedido) -> bool:
        tipo = pedido[0]
        if tipo == "desenhar":
            self._quadro = pedido[1]
            if self._quadro.aberta:
                self._redesenhar()
            else:
                self._esconder()
        elif tipo == "ouvir":
            self._soltar_atalho()
            try:
                self._agarrar_atalho(pedido[1])
            except Exception as exc:
                self._soltar_atalho()
                pedido[3].put(str(exc) or type(exc).__name__)
            else:
                self._ao_apertar = pedido[2]
                pedido[3].put("")
        elif tipo == "parar_de_ouvir":
            self._soltar_atalho()
        elif tipo == "ocultar":
            for janela in self._janelas.values():
                janela.unmap()
            if self._seta is not None:
                self._seta.unmap()
            # O print só pode sair depois que o servidor tirou as janelas.
            self._d.sync()
            time.sleep(0.03)
        elif tipo == "reexibir":
            # A tela mudou desde a última foto: compõe de novo em cima dela.
            if self._quadro is not None and self._quadro.aberta:
                self._redesenhar()
        elif tipo == "marca":
            pedido[1].set()
        elif tipo == "fim":
            return False
        self._d.flush()
        return True

    # O atalho
    def _agarrar_atalho(self, atalho: str) -> None:
        from Xlib import X, error

        mods, tecla = ler_combinacao(atalho)
        mascara = 0
        for mod in mods:
            mascara |= {"ctrl": X.ControlMask, "alt": X.Mod1Mask, "shift": X.ShiftMask, "win": X.Mod4Mask}[mod]
        codigo = self._d.keysym_to_keycode(_keysym_de_nome(tecla))
        # Outro programa com a mesma combinação: o X responde BadAccess, e sem
        # pegar o erro aqui a moldura abriria sem freio.
        pego = error.CatchError(error.BadAccess)
        # Com CapsLock ou NumLock ligado a máscara muda; sem estas variações o
        # atalho só funcionaria com as duas desligadas.
        for extra in (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask):
            self._raiz.grab_key(codigo, mascara | extra, True, X.GrabModeAsync, X.GrabModeAsync, onerror=pego)
            self._agarrados.append((codigo, mascara | extra))
        self._d.sync()
        if pego.get_error():
            raise ErroDoComputador("outro programa já usa essa combinação")
        self.atalho = atalho

    def _soltar_atalho(self) -> None:
        for codigo, mascara in self._agarrados:
            try:
                self._raiz.ungrab_key(codigo, mascara)
            except Exception:
                pass
        self._agarrados = []
        self._ao_apertar = None
        self.atalho = None
        try:
            self._d.sync()
        except Exception:
            pass

    # A moldura
    def _mouse(self) -> Tuple[int, int]:
        ponteiro = self._raiz.query_pointer()
        return ponteiro.root_x, ponteiro.root_y

    def _olhar_o_mouse(self) -> None:
        """A caixinha troca de canto e a pílula desce quando o mouse chega perto."""
        from .computador_desenho import onde_esta_o_mouse

        tela = self._d.screen()
        perto = onde_esta_o_mouse(self._mouse(), (tela.width_in_pixels, tela.height_in_pixels), self._quadro)
        if self._perto is not None and perto != self._perto:
            self._redesenhar()

    def _janela(self):
        from Xlib import X

        janela = self._raiz.create_window(
            0, 0, 1, 1, 0, self._d.screen().root_depth, X.InputOutput, X.CopyFromParent,
            override_redirect=True, event_mask=X.ExposureMask, background_pixmap=X.NONE)
        # Clique passa: a região de entrada vazia (extensão SHAPE).
        try:
            from Xlib.ext import shape

            janela.shape_rectangles(shape.SO.Set, shape.SK.Input, X.Unsorted, 0, 0, [])
        except Exception:
            logger.debug("Sem SHAPE: a moldura X11 vai receber clique.", exc_info=True)
        return janela

    def _redesenhar(self) -> None:
        from PIL import Image, ImageGrab

        from .computador_desenho import desenhar, onde_esta_o_mouse

        tela = self._d.screen()
        w, h = tela.width_in_pixels, tela.height_in_pixels
        mouse = self._mouse()
        self._perto = onde_esta_o_mouse(mouse, (w, h), self._quadro)
        camada, regioes = desenhar((w, h), self._quadro, mouse, brilho=False)
        for janela in self._janelas.values():
            janela.unmap()
        if self._seta is not None:
            self._seta.unmap()
        self._d.sync()
        time.sleep(0.03)
        fundo = ImageGrab.grab(xdisplay=self._nome).convert("RGBA")
        composta = Image.alpha_composite(fundo, camada).convert("RGB")
        vistas = []
        for regiao in regioes:
            janela = self._janelas.get(regiao.nome)
            if janela is None:
                janela = self._janelas[regiao.nome] = self._janela()
            janela.configure(x=regiao.x, y=regiao.y, width=regiao.largura, height=regiao.altura)
            self._imagens[regiao.nome] = composta.crop(
                (regiao.x, regiao.y, regiao.x + regiao.largura, regiao.y + regiao.altura))
            vistas.append(regiao.nome)
        for nome in [n for n in self._janelas if n not in vistas]:
            self._janelas.pop(nome).destroy()
            self._imagens.pop(nome, None)
        for nome in vistas:
            self._janelas[nome].map()
            self._janelas[nome].raise_window()
        self._d.sync()
        for nome in vistas:
            self._pintar(nome)
        self._mostrar_a_seta(mouse)
        self._d.sync()
        self.visivel = True

    # Cursor visual do agente ativo.
    def _mostrar_a_seta(self, mouse: Tuple[int, int]) -> None:
        """Mostra a seta do agente e oculta o cursor original com XFixes."""
        from PIL import Image
        from Xlib.ext import shape

        from .computador_desenho import seta as desenhar_seta

        if self._seta is None:
            seta, self._quente = desenhar_seta(32)
            w, h = seta.size
            janela = self._janela()
            janela.configure(width=w, height=h)
            mascara = seta.split()[3].point(lambda a: 255 if a > 110 else 0).convert("1")
            forma = self._raiz.create_pixmap(w, h, 1)
            gc = forma.create_gc(foreground=1, background=0)
            forma.put_pil_image(gc, 0, 0, mascara)
            gc.free()
            janela.shape_mask(shape.SO.Set, shape.SK.Bounding, 0, 0, forma)
            forma.free()
            fundo = Image.new("RGB", (w, h), (0x18, 0x18, 0x1B))
            fundo.paste(seta.convert("RGB"), mask=seta.split()[3])
            self._seta, self._imagem_da_seta = janela, fundo
            try:
                self._d.xfixes_query_version()
                self._raiz.xfixes_hide_cursor()
                self._cursor_escondido = True
            except Exception:
                logger.debug("Sem XFixes: o cursor de verdade fica junto da seta.", exc_info=True)
        self._onde_a_seta_esta = None
        self._seguir_o_mouse(mouse)
        self._seta.map()
        self._seta.raise_window()
        self._pintar_a_seta()

    def _seguir_o_mouse(self, mouse: Optional[Tuple[int, int]] = None) -> None:
        mouse = mouse or self._mouse()
        if mouse == self._onde_a_seta_esta:
            return
        self._onde_a_seta_esta = mouse
        self._seta.configure(x=mouse[0] - self._quente[0], y=mouse[1] - self._quente[1])
        self._seta.raise_window()
        self._d.flush()

    def _pintar_a_seta(self) -> None:
        if self._seta is None or self._imagem_da_seta is None:
            return
        gc = self._seta.create_gc()
        try:
            self._seta.put_pil_image(gc, 0, 0, self._imagem_da_seta)
        finally:
            gc.free()

    def _tirar_a_seta(self) -> None:
        if self._seta is not None:
            try:
                self._seta.destroy()
            except Exception:
                pass
        self._seta, self._imagem_da_seta, self._onde_a_seta_esta = None, None, None
        if self._cursor_escondido:
            try:
                self._raiz.xfixes_show_cursor()
            except Exception:
                pass
            self._cursor_escondido = False

    def _pintar(self, nome: str) -> None:
        janela, imagem = self._janelas.get(nome), self._imagens.get(nome)
        if janela is None or imagem is None:
            return
        gc = janela.create_gc()
        try:
            janela.put_pil_image(gc, 0, 0, imagem)
        finally:
            gc.free()

    def _esconder(self) -> None:
        for janela in self._janelas.values():
            try:
                janela.destroy()
            except Exception:
                pass
        self._janelas, self._imagens = {}, {}
        self._tirar_a_seta()
        self.visivel = False
        self._perto = None
        try:
            self._d.sync()
        except Exception:
            pass


__all__ = ["BackendX11"]
