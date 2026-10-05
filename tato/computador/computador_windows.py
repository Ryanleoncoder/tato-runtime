"""Opera o computador no Windows por meio das APIs do sistema.

Usa `SendInput` para gestos e Pillow para capturar a tela principal.
Apresenta a moldura numa janela transparente a cliques.
Registra o atalho de parada antes de permitir controle do computador.
"""
from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
from ctypes import wintypes
from typing import Callable, Dict, List, Optional, Tuple

from .computador_base import (
    BackendDoComputador, ErroDoComputador, Tela, gravando, ler_combinacao, tela_de_imagem,
)

logger = logging.getLogger(__name__)

VK = {
    "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "espaco": 0x20, "backspace": 0x08,
    "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "cima": 0x26, "baixo": 0x28, "esquerda": 0x25, "direita": 0x27,
    "capslock": 0x14, "printscreen": 0x2C, "menu": 0x5D,
    **{f"f{n}": 0x6F + n for n in range(1, 25)},
    "=": 0xBB, "-": 0xBD, ",": 0xBC, ".": 0xBE,
}
# Teclas que o Windows marca como "estendidas": sem a marca, a seta vira o
# número do teclado numérico com NumLock ligado.
ESTENDIDAS = frozenset({"delete", "insert", "home", "end", "pageup", "pagedown",
                        "cima", "baixo", "esquerda", "direita", "win", "menu", "printscreen"})

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0001, 0x0002, 0x0004
MOUSEEVENTF_MOVE, MOUSEEVENTF_ABSOLUTE = 0x0001, 0x8000
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x01000
_BOTAO = {"esquerdo": (0x0002, 0x0004), "direito": (0x0008, 0x0010), "meio": (0x0020, 0x0040)}
WHEEL_DELTA = 120

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_TIMER, WM_DESTROY, WM_NCHITTEST, WM_APP = 0x0312, 0x0113, 0x0002, 0x0084, 0x8000
HTTRANSPARENT = -1
WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST = 0x00080000, 0x00000020, 0x00000008
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000
ULW_ALPHA = 0x2
SPI_SETCURSORS, SPI_GETWORKAREA = 0x0057, 0x0030
HWND_TOPMOST = -1
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
# Os cursores que viram a seta: sem o de texto e o de link, o cursor voltaria
# a ser o da pessoa em cima de um campo ou de um link.
OCR_NORMAL, OCR_IBEAM, OCR_HAND = 32512, 32513, 32649
CURSORES_TROCADOS = (OCR_NORMAL, OCR_IBEAM, OCR_HAND)
WDA_EXCLUDEFROMCAPTURE = 0x11
SW_HIDE, SW_SHOWNOACTIVATE, SW_RESTORE = 0, 4, 9
_ID_DO_ATALHO = 0x5E17

def unidades_utf16(texto: str) -> List[int]:
    """O `KEYEVENTF_UNICODE` recebe unidades UTF-16: emoji fora do plano
    básico vai em dois eventos (o par substituto)."""
    dados = str(texto).encode("utf-16-le")
    return [int.from_bytes(dados[i:i + 2], "little") for i in range(0, len(dados), 2)]


def pela_tecla(varredura: int, caps_ligado: bool = False, caractere: str = "") -> Optional[Tuple[int, bool]]:
    """O que o `VkKeyScanW` devolve para um caractere → (vk, com_shift), ou
    None quando ele não tem tecla própria no layout: sem tecla (-1), com AltGr
    ou Ctrl, ou letra com o CapsLock ligado (o shift sairia invertido). O None
    vai pelo `KEYEVENTF_UNICODE`.

    Pela tecla do layout, o app recebe o evento que um teclado de verdade
    manda (código da tecla, shift junto); o `KEYEVENTF_UNICODE` chega como
    `VK_PACKET`, que nenhum teclado físico produz."""
    varredura = int(varredura) & 0xFFFF
    if varredura == 0xFFFF:
        return None
    vk, estado = varredura & 0xFF, (varredura >> 8) & 0xFF
    if estado & ~0x01:
        return None
    if caps_ligado and caractere.isalpha():
        return None
    return vk, bool(estado & 0x01)


def modificadores_do_atalho(atalho: str) -> Tuple[int, int]:
    mods, tecla = ler_combinacao(atalho)
    mascara = MOD_NOREPEAT
    for mod in mods:
        mascara |= {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}[mod]
    return mascara, vk_de(tecla)


def vk_de(tecla: str) -> int:
    if tecla in VK:
        return VK[tecla]
    if len(tecla) == 1 and tecla.isascii() and tecla.isalnum():
        return ord(tecla.upper())
    raise ErroDoComputador(f"tecla sem código no Windows: {tecla!r}")


def normalizar(valor: int, tamanho: int) -> int:
    """Pixel → 0..65535, a escala do `MOUSEEVENTF_ABSOLUTE`."""
    return round(max(0, min(valor, tamanho - 1)) * 65535 / max(1, tamanho - 1))


# Larguras fixas, e nao `wintypes.LONG`/`DWORD`: no Linux esses sao `c_long`,
# de 8 bytes, e a estrutura sairia com outro tamanho. Assim o layout e o do
# Windows em qualquer lugar, e o teste do tamanho vale tambem na nuvem.
class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32), ("mouseData", ctypes.c_uint32),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16), ("wParamH", ctypes.c_uint16)]


class _UNIAO(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.c_uint32), ("u", _UNIAO)]


def _api():
    """As DLLs, só no Windows. `use_last_error` para o erro dizer o motivo."""
    if not sys.platform.startswith("win"):
        raise ErroDoComputador("o backend do Windows só roda no Windows")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
    user32.VkKeyScanW.restype = ctypes.c_short
    user32.GetKeyState.argtypes = [ctypes.c_int]
    user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    user32.MapVirtualKeyW.restype = wintypes.UINT
    user32.GetKeyState.restype = ctypes.c_short
    user32.SendInput.restype = wintypes.UINT
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = wintypes.LPARAM
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    # Tudo que é ponteiro ou alça precisa do tipo declarado: sem isso o ctypes
    # passa int de 32 bits e, em 64 bits, a alça sai cortada.
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.UpdateLayeredWindow.argtypes = [wintypes.HWND, wintypes.HDC, ctypes.c_void_p, ctypes.c_void_p,
                                           wintypes.HDC, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                           wintypes.DWORD]
    user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP
    gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [wintypes.HDC, ctypes.c_void_p]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.CreateBitmap.restype = wintypes.HBITMAP
    gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT, ctypes.c_void_p]
    user32.CreateIconIndirect.restype = wintypes.HICON
    user32.CreateIconIndirect.argtypes = [ctypes.c_void_p]
    user32.CopyIcon.restype = wintypes.HICON
    user32.CopyIcon.argtypes = [wintypes.HICON]
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.SetSystemCursor.argtypes = [wintypes.HICON, wintypes.DWORD]
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    for nome in ("IsIconic", "SetForegroundWindow", "BringWindowToTop", "IsWindowVisible"):
        getattr(user32, nome).argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, wintypes.UINT]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    return user32, gdi32, kernel32


class BackendWindows(BackendDoComputador):
    nome = "windows"

    def __init__(self) -> None:
        super().__init__()
        self._user32, self._gdi32, self._kernel32 = _api()
        # Coordenada em pixel físico: sem isto, numa tela a 150%, o print e o
        # clique discordam pelo fator de escala.
        try:
            self._user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        except Exception:
            try:
                self._user32.SetProcessDPIAware()
            except Exception:
                pass
        self._trava = threading.RLock()
        self._palco: Optional[_PalcoWindows] = None
        if _marca_do_cursor().exists():
            logger.warning("A seta do agente ficou no lugar do cursor numa queda anterior; devolvendo.")
            devolver_cursor(self._user32)
        import atexit

        user32 = self._user32
        # Saída normal com a seta no lugar do cursor: devolve antes de sair.
        atexit.register(lambda: _marca_do_cursor().exists() and devolver_cursor(user32))

    # Leitura
    def tamanho(self) -> Tuple[int, int]:
        return self._user32.GetSystemMetrics(0), self._user32.GetSystemMetrics(1)

    def _capturar(self, caixa=None):
        """Captura a tela, ou uma caixa dela, sem a moldura.

        Usa `WDA_EXCLUDEFROMCAPTURE`; sem ele, esconde a moldura durante a captura.
        Gravando, a moldura fica: esconder e mostrar piscaria no vídeo.
        """
        from PIL import ImageGrab

        palco = self._palco
        esconder = palco is not None and palco.visivel and not palco.fora_da_captura and not gravando()
        if esconder:
            palco.pedir("ocultar")
            palco.sincronizar()
        try:
            return ImageGrab.grab(bbox=caixa)
        except Exception as exc:
            raise ErroDoComputador(f"não consegui tirar o print: {exc}") from exc
        finally:
            if esconder:
                palco.pedir("reexibir")

    def recorte(self, caixa: Tuple[int, int, int, int]) -> Tela:
        from .computador_base import ampliar

        imagem = self._capturar(caixa)
        return ampliar(imagem, caixa[2] - caixa[0], caixa[3] - caixa[1])

    def texto(self, maximo: int = 12000) -> str:
        from .computador_uia import texto_da_janela

        return texto_da_janela(maximo)

    def tela(self) -> Tela:
        """O print é da tela, não da moldura."""
        palco = self._palco
        imagem = self._capturar()
        # O ImageGrab não traz o cursor: sem ele, ninguém sabe onde o mouse está.
        from .computador_desenho import com_cursor
        from .computador_moldura import agente

        return tela_de_imagem(com_cursor(imagem, self.posicao_do_mouse(),
                                         com_seta=palco is not None and palco.visivel, agente=agente()))

    def posicao_do_mouse(self) -> Tuple[int, int]:
        ponto = wintypes.POINT()
        self._user32.GetCursorPos(ctypes.byref(ponto))
        return ponto.x, ponto.y

    def programa_da_frente(self) -> Optional[str]:
        from .computador_programas import executavel_do_processo

        janela = self._user32.GetForegroundWindow()
        if not janela:
            return None
        processo = wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(janela, ctypes.byref(processo))
        if not processo.value:
            return None
        # Compara pelo executável: diálogos e pop-ups do Chrome rodam em outro chrome.exe.
        return executavel_do_processo(processo.value).lower() or f"pid:{processo.value}"

    def elementos(self) -> list:
        from .computador_uia import elementos

        return elementos()

    def foco(self):
        from .computador_uia import foco

        try:
            return foco()
        except ErroDoComputador:
            return None

    def invocar(self, alvo) -> str:
        from .computador_uia import invocar

        return invocar(alvo)

    def focar_elemento(self, alvo) -> None:
        from .computador_uia import focar

        focar(alvo)

    def lista_editavel(self, alvo) -> bool:
        from .computador_uia import lista_editavel

        return lista_editavel(alvo)

    def escolher_opcao(self, alvo, opcao: str) -> None:
        from .computador_uia import escolher_opcao

        escolher_opcao(alvo, opcao)

    def programas(self, nome: str = "") -> Dict[str, object]:
        """Agrupa as janelas pelo executável do processo dono, minimizadas
        inclusive. Com `nome`, procura também entre os processos sem janela (a
        bandeja) e os atalhos do menu Iniciar."""
        from . import computador_programas as cp

        u = self._user32
        frente = u.GetForegroundWindow()
        moldura = self._palco._hwnd if self._palco is not None else None
        por_programa: Dict[str, List[Dict[str, object]]] = {}
        pids: set = set()
        tipo = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # type: ignore[attr-defined]

        def visitar(hwnd, _):
            tamanho = u.GetWindowTextLengthW(hwnd)
            if not tamanho or not u.IsWindowVisible(hwnd) or (moldura and hwnd == moldura):
                return True
            if cp.escondida_pelo_sistema(hwnd):
                return True
            titulo = ctypes.create_unicode_buffer(tamanho + 1)
            u.GetWindowTextW(hwnd, titulo, tamanho + 1)
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pids.add(pid.value)
            programa = cp.executavel_do_processo(pid.value) or "desconhecido"
            por_programa.setdefault(programa, []).append(
                {"titulo": titulo.value, "em_foco": hwnd == frente, "minimizada": bool(u.IsIconic(hwnd))})
            return True

        u.EnumWindows(tipo(visitar), 0)
        saida: Dict[str, object] = {"abertos": [{"programa": p, "janelas": j} for p, j in sorted(por_programa.items())]}
        if nome:
            procurado = cp._normal(nome)
            saida["sem_janela"] = sorted({exe for pid, exe in cp.processos().items()
                                          if procurado in cp._normal(exe) and pid not in pids})
            saida["instalados"] = [i["nome"] for i in cp.instalados(nome)]
        return saida

    def abrir(self, nome: str, interromper: Callable[[], bool] = lambda: False) -> str:
        import time

        from . import computador_programas as cp

        antes = [str(j.get("titulo") or "") for j in self.janelas()]

        def janela_do_programa(procurado: str) -> str:
            chave = cp._normal(procurado)
            for grupo in self.programas()["abertos"]:
                if chave in cp._normal(grupo["programa"]) or any(chave in cp._normal(j["titulo"])
                                                                   for j in grupo["janelas"]):
                    return str(grupo["janelas"][0]["titulo"])
            agora = [str(j.get("titulo") or "") for j in self.janelas()]
            return cp.janela_nova_do_atalho(procurado, antes, agora)

        try:
            return cp.abrir_pelo_menu(nome, apertar=self.combinacao, escrever=self.escrever,
                                      janela_do_programa=janela_do_programa, esperar=time.sleep,
                                      focar=self.focar, interromper=interromper)
        except LookupError as exc:
            raise ErroDoComputador(str(exc)) from exc

    def fechar_janela(self, titulo: str) -> str:
        """Manda `WM_CLOSE`, como o X da janela: o programa ainda pergunta se quer
        salvar. A área de trabalho e a barra ficam de fora, porque fechar a área
        de trabalho abre o desligar do Windows."""
        u = self._user32
        procurado = " ".join(str(titulo or "").split()).lower()
        if not procurado:
            raise ErroDoComputador("fechar precisa de `janela`, parte do título como aparece em `janelas`")
        hwnd, nome = self._achar_janela(procurado, titulo)
        classe = ctypes.create_unicode_buffer(64)
        u.GetClassNameW(hwnd, classe, 64)
        if classe.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            raise ErroDoComputador(f"«{nome}» é a área de trabalho ou a barra de tarefas; isso não se fecha")
        u.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return nome

    def focar(self, titulo: str) -> str:
        """A janela vem para a frente sem tecla nenhuma: o Windows só entrega o
        foco a quem está na fila de entrada da janela em foco, então a thread
        se junta a ela durante a troca."""
        u, k = self._user32, self._kernel32
        procurado = " ".join(str(titulo or "").split()).lower()
        if not procurado:
            raise ErroDoComputador("focar precisa de `janela`, parte do título como aparece em `janelas`")
        hwnd, nome = self._achar_janela(procurado, titulo)
        if u.IsIconic(hwnd):
            u.ShowWindow(hwnd, SW_RESTORE)
        frente = u.GetForegroundWindow()
        minha = k.GetCurrentThreadId()
        dela = u.GetWindowThreadProcessId(frente, None) if frente else 0
        juntou = bool(dela and dela != minha and u.AttachThreadInput(minha, dela, True))
        try:
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
        finally:
            if juntou:
                u.AttachThreadInput(minha, dela, False)
        if u.GetForegroundWindow() != hwnd:
            raise ErroDoComputador(f"o Windows não deixou trazer «{nome}» para a frente; clique nela na barra de tarefas")
        return nome

    def _achar_janela(self, procurado: str, titulo: str) -> Tuple[int, str]:
        """A janela visível com `procurado` no título: a de título igual vence,
        senão a mais de cima."""
        u = self._user32
        moldura = self._palco._hwnd if self._palco is not None else None
        achadas: List[Tuple[int, str]] = []
        tipo = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # type: ignore[attr-defined]

        def visitar(hwnd, _):
            tamanho = u.GetWindowTextLengthW(hwnd)
            if not tamanho or not u.IsWindowVisible(hwnd) or (moldura and hwnd == moldura):
                return True
            texto = ctypes.create_unicode_buffer(tamanho + 1)
            u.GetWindowTextW(hwnd, texto, tamanho + 1)
            if procurado in texto.value.lower():
                achadas.append((hwnd, texto.value))
            return True

        u.EnumWindows(tipo(visitar), 0)
        if not achadas:
            raise ErroDoComputador(
                f"nenhuma janela aberta com «{titulo}» no título: o app não está aberto (ou tem outro nome). "
                "`programas` com `nome` diz se ele roda sem janela e se está instalado; instalado e fechado, "
                "`abrir` com o `nome` abre, e ele demora alguns segundos para aparecer em `janelas`")
        return next((a for a in achadas if a[1].lower() == procurado), achadas[0])

    def janelas_visiveis(self) -> Optional[set]:
        user32 = self._user32
        vistas: set = set()
        tipo = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # type: ignore[attr-defined]

        def visitar(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                vistas.add(int(hwnd or 0))
            return True

        user32.EnumWindows(tipo(visitar), 0)
        return vistas

    def janela_da_frente(self) -> Optional[int]:
        return int(self._user32.GetForegroundWindow() or 0) or None

    def janelas(self) -> List[Dict[str, object]]:
        user32 = self._user32
        frente = user32.GetForegroundWindow()
        saida: List[Dict[str, object]] = []
        tipo = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # type: ignore[attr-defined]

        moldura = self._palco._hwnd if self._palco is not None else None

        from .computador_programas import escondida_pelo_sistema

        def visitar(hwnd, _):
            if not user32.IsWindowVisible(hwnd) or (moldura and hwnd == moldura):
                return True
            # Ignora o shell e as janelas que o sistema mantém ocultas.
            if hwnd != frente and escondida_pelo_sistema(hwnd):
                return True
            tamanho = user32.GetWindowTextLengthW(hwnd)
            if tamanho == 0:
                return True
            titulo = ctypes.create_unicode_buffer(tamanho + 1)
            user32.GetWindowTextW(hwnd, titulo, tamanho + 1)
            caixa = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(caixa))
            if caixa.right - caixa.left <= 0 or caixa.bottom - caixa.top <= 0:
                return True
            saida.append({"titulo": titulo.value, "em_foco": hwnd == frente, "x": caixa.left, "y": caixa.top,
                          "largura": caixa.right - caixa.left, "altura": caixa.bottom - caixa.top})
            return True

        user32.EnumWindows(tipo(visitar), 0)
        return saida

    # Gestos
    def _enviar(self, *entradas: _INPUT) -> None:
        vetor = (_INPUT * len(entradas))(*entradas)
        with self._trava:
            enviadas = self._user32.SendInput(len(entradas), vetor, ctypes.sizeof(_INPUT))
        if enviadas != len(entradas):
            # Erro 5 indica que a janela em foco pode exigir privilégios elevados.
            raise ErroDoComputador(
                f"o Windows recusou o gesto (SendInput {enviadas}/{len(entradas)}, erro {ctypes.get_last_error()}); "
                "se a janela em foco roda como administrador, o Tato não pode mexer nela")

    def _mouse(self, flags: int, dx: int = 0, dy: int = 0, dados: int = 0) -> _INPUT:
        entrada = _INPUT(type=INPUT_MOUSE)
        entrada.mi = _MOUSEINPUT(dx, dy, ctypes.c_uint32(dados).value, flags, 0, 0)
        return entrada

    def _teclado(self, vk: int = 0, scan: int = 0, flags: int = 0) -> _INPUT:
        if vk and not scan and not flags & KEYEVENTF_UNICODE:
            # Envia o scan code como um teclado físico; jogos e emuladores leem por ele.
            scan = int(self._user32.MapVirtualKeyW(vk, 0)) & 0xFFFF
        entrada = _INPUT(type=INPUT_KEYBOARD)
        entrada.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
        return entrada

    def mover(self, x: int, y: int) -> None:
        largura, altura = self.tamanho()
        self._enviar(self._mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
                                 normalizar(int(x), largura), normalizar(int(y), altura)))

    def botao(self, botao: str, apertar: bool) -> None:
        if botao not in _BOTAO:
            raise ErroDoComputador(f"botão desconhecido: {botao!r}")
        self._enviar(self._mouse(_BOTAO[botao][0 if apertar else 1]))
        (self._apertou if apertar else self._soltou)(f"botao:{botao}")

    def tecla_bruta(self, tecla: str, apertar: bool) -> None:
        flags = (0 if apertar else KEYEVENTF_KEYUP) | (KEYEVENTF_EXTENDEDKEY if tecla in ESTENDIDAS else 0)
        self._enviar(self._teclado(vk=vk_de(tecla), flags=flags))
        (self._apertou if apertar else self._soltou)(f"tecla:{tecla}")

    def escrever(self, texto: str) -> None:
        entradas: List[_INPUT] = []
        for c in str(texto):
            if c in "\r\n":
                if c == "\n":
                    entradas += [self._teclado(vk=VK["enter"]), self._teclado(vk=VK["enter"], flags=KEYEVENTF_KEYUP)]
                continue
            tecla = None
            if len(unidades_utf16(c)) == 1:
                caps = bool(self._user32.GetKeyState(VK["capslock"]) & 1)
                tecla = pela_tecla(self._user32.VkKeyScanW(c), caps, c)
            if tecla is not None:
                vk, shift = tecla
                if shift:
                    entradas.append(self._teclado(vk=VK["shift"]))
                entradas += [self._teclado(vk=vk), self._teclado(vk=vk, flags=KEYEVENTF_KEYUP)]
                if shift:
                    entradas.append(self._teclado(vk=VK["shift"], flags=KEYEVENTF_KEYUP))
                continue
            for unidade in unidades_utf16(c):
                entradas += [self._teclado(scan=unidade, flags=KEYEVENTF_UNICODE),
                             self._teclado(scan=unidade, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
        for i in range(0, len(entradas), 200):  # em lotes: SendInput tem teto prático
            self._enviar(*entradas[i:i + 200])

    def roda(self, dx: int, dy: int) -> None:
        if dy:
            self._enviar(self._mouse(MOUSEEVENTF_WHEEL, dados=-WHEEL_DELTA * int(dy)))
        if dx:
            self._enviar(self._mouse(MOUSEEVENTF_HWHEEL, dados=WHEEL_DELTA * int(dx)))

    # A moldura e o atalho
    def _palco_vivo(self) -> "_PalcoWindows":
        if self._palco is None or not self._palco.is_alive():
            self._palco = _PalcoWindows(self._user32, self._gdi32, self._kernel32)
            self._palco.start()
            if not self._palco.pronto.wait(5) or self._palco.erro:
                raise ErroDoComputador(f"a moldura não abriu: {self._palco.erro or 'sem resposta'}")
        return self._palco

    def desenhar(self, quadro) -> None:
        palco = self._palco_vivo()
        palco.pedir("desenhar", quadro)
        if not quadro.aberta:
            # A seta muda o cursor do Windows inteiro. Ao encerrar o turno,
            # esperar a thread da moldura devolve-lo antes de prosseguir.
            palco.sincronizar()

    def ouvir_atalho(self, atalho: str, ao_apertar: Callable[[], None]) -> None:
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

    def fechar(self) -> None:
        if self._palco is not None:
            self._palco.pedir("fim")
            self._palco.join(2)
            self._palco = None


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32), ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16), ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 1)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_int32), ("cy", ctypes.c_int32)]


class _PONTO(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _ICONINFO(ctypes.Structure):
    _fields_ = [("fIcon", ctypes.c_int32), ("xHotspot", ctypes.c_uint32), ("yHotspot", ctypes.c_uint32),
                ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]


def bgra_reto(imagem) -> bytes:
    """RGBA → BGRA sem pré-multiplicar: o formato do bitmap de cor de um
    cursor de 32 bits (o do `UpdateLayeredWindow` é o outro)."""
    from PIL import Image

    r, g, b, a = imagem.convert("RGBA").split()
    return Image.merge("RGBA", (b, g, r, a)).tobytes("raw", "RGBA")


def _marca_do_cursor():
    """Localiza o marcador usado para restaurar o cursor após uma queda."""
    import tempfile
    from pathlib import Path

    return Path(tempfile.gettempdir()) / "tato-cursor-trocado"


def devolver_cursor(user32) -> bool:
    """Recarrega os cursores do esquema da pessoa (o que está no registro)."""
    restaurado = bool(user32.SystemParametersInfoW(SPI_SETCURSORS, 0, None, 0))
    if restaurado:
        try:
            _marca_do_cursor().unlink()
        except OSError:
            pass
    else:
        logger.warning("O Windows recusou restaurar o cursor; marcador preservado para tentar novamente.")
    return restaurado


def bgra_premultiplicado(imagem) -> bytes:
    """RGBA do Pillow → BGRA com alfa pré-multiplicado, o formato que o
    `UpdateLayeredWindow` com `AC_SRC_ALPHA` exige. Sem pré-multiplicar, o
    que é translúcido sai claro demais."""
    from PIL import Image, ImageChops

    r, g, b, a = imagem.convert("RGBA").split()
    r, g, b = (ImageChops.multiply(canal, a) for canal in (r, g, b))
    return Image.merge("RGBA", (b, g, r, a)).tobytes("raw", "RGBA")


class _PalcoWindows(threading.Thread):
    """Mantém a janela da moldura e recebe o atalho registrado na thread.

    Usa alfa por pixel para desenhar a moldura sem interceptar cliques.
    """

    def __init__(self, user32, gdi32, kernel32) -> None:
        super().__init__(daemon=True, name="tato-moldura-windows")
        self.u, self.g, self.k = user32, gdi32, kernel32
        self.pronto = threading.Event()
        self.erro = ""
        self._pedidos: "queue.Queue" = queue.Queue()
        self._id_da_thread = 0
        self._hwnd = None
        self._quadro = None
        self._ao_apertar: Optional[Callable[[], None]] = None
        self._registrado = False
        self._perto: Optional[tuple] = None
        self._cursor_trocado = False
        self.fora_da_captura = False
        self.visivel = False

    def sincronizar(self, prazo: float = 2.0) -> None:
        feito = threading.Event()
        self.pedir("marca", feito)
        if not feito.wait(prazo):
            logger.warning("A thread da moldura nao confirmou a devolucao do cursor em %.1fs.", prazo)

    def pedir(self, *pedido) -> None:
        self._pedidos.put(pedido)
        if self._id_da_thread:
            self.u.PostThreadMessageW(self._id_da_thread, WM_APP, 0, 0)

    def run(self) -> None:
        u, k = self.u, self.k
        try:
            self._id_da_thread = k.GetCurrentThreadId()
            tipo = ctypes.WINFUNCTYPE(wintypes.LPARAM, wintypes.HWND, wintypes.UINT,  # type: ignore[attr-defined]
                                      wintypes.WPARAM, wintypes.LPARAM)
            self._proc = tipo(self._wndproc)  # a referência precisa viver com a janela

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", tipo), ("cbClsExtra", ctypes.c_int),
                            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                            ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                            ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                            ("lpszClassName", wintypes.LPCWSTR)]

            instancia = k.GetModuleHandleW(None)
            classe = WNDCLASSW(0, self._proc, 0, 0, instancia, None, None, None, None, "TatoMoldura")
            u.RegisterClassW(ctypes.byref(classe))
            largura, altura = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
            self._hwnd = u.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                "TatoMoldura", "Tato", WS_POPUP, 0, 0, largura, altura, None, None, instancia, None)
            if not self._hwnd:
                raise OSError(f"CreateWindowExW falhou (erro {ctypes.get_last_error()})")
            # Fora do print: o modelo vê a tela, não a moldura (Windows 10
            # 2004+). Sem isto, o `tela()` esconde e mostra em volta da captura.
            self.fora_da_captura = not gravando() and bool(
                u.SetWindowDisplayAffinity(self._hwnd, WDA_EXCLUDEFROMCAPTURE))
            # A caixinha troca de canto quando o mouse chega perto: olhar o
            # mouse de tempos em tempos (WM_TIMER).
            u.SetTimer(self._hwnd, 1, 150, None)
        except Exception as exc:
            self.erro = str(exc)
            self.pronto.set()
            return
        self.pronto.set()
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam == _ID_DO_ATALHO and self._ao_apertar is not None:
                # Em outra thread: o parar pede de volta a esta fila.
                threading.Thread(target=self._ao_apertar, daemon=True).start()
                continue
            if msg.message == WM_APP:
                self._atender_pedidos()
                continue
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

    def _atender_pedidos(self) -> None:
        u = self.u
        while True:
            try:
                pedido = self._pedidos.get_nowait()
            except queue.Empty:
                return
            tipo = pedido[0]
            try:
                if tipo == "desenhar":
                    self._quadro = pedido[1]
                    self.visivel = bool(self._quadro.aberta)
                    if self._quadro.aberta:
                        self._aplicar()
                        u.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
                        self._trocar_o_cursor()
                    else:
                        u.ShowWindow(self._hwnd, SW_HIDE)
                        self._perto = None
                        self._devolver_o_cursor()
                elif tipo == "ocultar":
                    u.ShowWindow(self._hwnd, SW_HIDE)
                    u.UpdateWindow(self._hwnd)
                elif tipo == "reexibir":
                    if self.visivel:
                        u.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
                elif tipo == "marca":
                    pedido[1].set()
                elif tipo == "ouvir":
                    self._soltar_atalho()
                    mascara, vk = modificadores_do_atalho(pedido[1])
                    if u.RegisterHotKey(None, _ID_DO_ATALHO, mascara, vk):
                        self._registrado, self._ao_apertar = True, pedido[2]
                        pedido[3].put("")
                    else:
                        pedido[3].put(f"outro programa já usa essa combinação (erro {ctypes.get_last_error()})")
                elif tipo == "parar_de_ouvir":
                    self._soltar_atalho()
                elif tipo == "fim":
                    self._soltar_atalho()
                    self._devolver_o_cursor()
                    u.DestroyWindow(self._hwnd)
                    return
            except Exception:
                logger.warning("Pedido da moldura do Windows falhou: %s", tipo, exc_info=True)

    def _trocar_o_cursor(self) -> None:
        """Troca o cursor do sistema pela seta do agente ativo."""
        if self._cursor_trocado:
            return
        from .computador_desenho import seta as desenhar_seta

        u, g = self.u, self.g
        seta, (qx, qy) = desenhar_seta(32)
        w, h = seta.size
        dados = bgra_reto(seta)
        info = _BITMAPINFO()
        info.bmiHeader = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        tela = u.GetDC(None)
        cor = g.CreateDIBSection(tela, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        u.ReleaseDC(None, tela)
        mascara = g.CreateBitmap(w, h, 1, 1, ctypes.create_string_buffer(((w + 15) // 16 * 2) * h))
        try:
            if not cor or not bits.value or not mascara:
                raise OSError(f"bitmap do cursor falhou (erro {ctypes.get_last_error()})")
            ctypes.memmove(bits, dados, len(dados))
            icone = u.CreateIconIndirect(ctypes.byref(_ICONINFO(False, qx, qy, mascara, cor)))
            if not icone:
                raise OSError(f"CreateIconIndirect falhou (erro {ctypes.get_last_error()})")
            _marca_do_cursor().write_text("1")
            # O SetSystemCursor fica com o cursor que recebe: um por tipo.
            for tipo in CURSORES_TROCADOS:
                u.SetSystemCursor(u.CopyIcon(icone), tipo)
            u.DestroyIcon(icone)
            self._cursor_trocado = True
        except Exception:
            logger.warning("Não consegui trocar o cursor pela seta do agente.", exc_info=True)
        finally:
            if cor:
                g.DeleteObject(cor)
            if mascara:
                g.DeleteObject(mascara)

    def _devolver_o_cursor(self) -> None:
        if self._cursor_trocado:
            if devolver_cursor(self.u):
                self._cursor_trocado = False

    def _soltar_atalho(self) -> None:
        if self._registrado:
            self.u.UnregisterHotKey(None, _ID_DO_ATALHO)
        self._registrado, self._ao_apertar = False, None

    def _wndproc(self, hwnd, mensagem, wparam, lparam):
        if mensagem == WM_NCHITTEST:
            return HTTRANSPARENT
        if mensagem == WM_TIMER and self.visivel:
            self._olhar_o_mouse()
            return 0
        if mensagem == WM_DESTROY:
            self.u.KillTimer(hwnd, 1)
            self.u.PostQuitMessage(0)
            return 0
        return self.u.DefWindowProcW(hwnd, mensagem, wparam, lparam)

    def _mouse(self) -> Tuple[int, int]:
        ponto = wintypes.POINT()
        self.u.GetCursorPos(ctypes.byref(ponto))
        return ponto.x, ponto.y

    def _rodape(self) -> int:
        """Quanto a barra de tarefas ocupa embaixo: a caixinha das falas fica
        acima dela, porque a barra e o Iniciar sobem por cima de qualquer
        janela "sempre no topo" quando alguém os usa."""
        caixa = wintypes.RECT()
        altura = self.u.GetSystemMetrics(1)
        if self.u.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(caixa), 0) and 0 < caixa.bottom <= altura:
            return altura - caixa.bottom
        return 0

    def _olhar_o_mouse(self) -> None:
        from .computador_desenho import onde_esta_o_mouse

        tela = (self.u.GetSystemMetrics(0), self.u.GetSystemMetrics(1))
        mouse = self._mouse()
        perto = onde_esta_o_mouse(mouse, tela, self._quadro, self._rodape())
        segue = bool(self._quadro.etiqueta) and self._quadro.ponto is None
        if ((self._perto is not None and perto != self._perto)
                or (segue and mouse != getattr(self, "_mouse_desenhado", None))):
            self._aplicar()

    def _aplicar(self) -> None:
        """O desenho inteiro na janela, com alfa por pixel. Como o
        compartilhamento de tela do Teams: borda sólida na tela toda, sem brilho."""
        from .computador_desenho import desenhar, onde_esta_o_mouse

        u, g = self.u, self.g
        largura, altura = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        rodape = self._rodape()
        mouse = self._mouse()
        self._mouse_desenhado = mouse
        self._perto = onde_esta_o_mouse(mouse, (largura, altura), self._quadro, rodape)
        camada, _ = desenhar((largura, altura), self._quadro, mouse, brilho=False, rodape=rodape)
        dados = bgra_premultiplicado(camada)

        info = _BITMAPINFO()
        info.bmiHeader = _BITMAPINFOHEADER(ctypes.sizeof(_BITMAPINFOHEADER), largura, -altura, 1, 32, 0,
                                           0, 0, 0, 0, 0)  # altura negativa: linhas de cima para baixo
        bits = ctypes.c_void_p()
        tela = u.GetDC(None)
        memoria = g.CreateCompatibleDC(tela)
        mapa = g.CreateDIBSection(tela, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        try:
            if not mapa or not bits.value:
                raise OSError(f"CreateDIBSection falhou (erro {ctypes.get_last_error()})")
            ctypes.memmove(bits, dados, len(dados))
            anterior = g.SelectObject(memoria, mapa)
            mistura = _BLENDFUNCTION(0, 0, 255, 1)  # AC_SRC_OVER, alfa por pixel (AC_SRC_ALPHA)
            ok = u.UpdateLayeredWindow(self._hwnd, tela, ctypes.byref(_PONTO(0, 0)),
                                       ctypes.byref(_SIZE(largura, altura)), memoria,
                                       ctypes.byref(_PONTO(0, 0)), 0, ctypes.byref(mistura), ULW_ALPHA)
            g.SelectObject(memoria, anterior)
            if not ok:
                logger.warning("UpdateLayeredWindow falhou (erro %s)", ctypes.get_last_error())
            # A barra de tarefas e outra janela "sempre no topo" podem ter subido
            # desde o último desenho: a borda volta por cima delas.
            u.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        finally:
            if mapa:
                g.DeleteObject(mapa)
            g.DeleteDC(memoria)
            u.ReleaseDC(None, tela)


__all__ = ["BackendWindows", "bgra_premultiplicado", "bgra_reto", "devolver_cursor", "modificadores_do_atalho", "normalizar", "unidades_utf16",
           "vk_de"]
