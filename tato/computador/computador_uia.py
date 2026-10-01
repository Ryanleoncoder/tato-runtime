"""Lê controles do Windows pela UI Automation.

Usa `comtypes` para obter nome, papel e posição em pixels físicos.
Organiza menus, janela em foco e demais janelas pela ordem visual.
Sem acessibilidade disponível, mantém o controle por coordenadas da imagem.
"""
from __future__ import annotations

import logging
import threading
from typing import List, Optional, Tuple

from .computador_base import ErroDoComputador
from .computador_elementos import Camada, Elemento, juntar

logger = logging.getLogger(__name__)

_CLSID_CUIAUTOMATION = "{ff48dba4-60ef-4201-aa87-54103eef594e}"
_TREE_SCOPE_DESCENDANTS = 4
_UIA_CONTROL_TYPE = 30003
_UIA_NAME = 30005
_UIA_BOUNDING_RECTANGLE = 30001
_UIA_IS_ENABLED = 30010
_UIA_IS_OFFSCREEN = 30022
_UIA_IS_PASSWORD = 30019
_UIA_IS_CONTROL_ELEMENT = 30016
_UIA_VALUE_VALUE = 30045
_UIA_TOGGLE_STATE = 30086
_UIA_SELECTION_ITEM_IS_SELECTED = 30079
_UIA_EXPAND_COLLAPSE_STATE = 30070
_ESTADOS_EXTRAS = (_UIA_VALUE_VALUE, _UIA_TOGGLE_STATE, _UIA_SELECTION_ITEM_IS_SELECTED, _UIA_EXPAND_COLLAPSE_STATE)

_PAPEIS = {
    50000: "botao", 50031: "botao",               # Button, SplitButton
    50004: "campo",                               # Edit (senha vem pelo IsPassword)
    50005: "link",                                # Hyperlink
    50002: "caixa",                               # CheckBox
    50013: "opcao",                               # RadioButton
    50003: "lista",                               # ComboBox
    50007: "item", 50024: "item", 50029: "item",  # ListItem, TreeItem, DataItem
    50011: "menu", 50009: "menu",                 # MenuItem, Menu
    50019: "aba",                                 # TabItem
    50015: "controle", 50014: "controle",         # Slider, ScrollBar
}

_WS_POPUP = 0x80000000
_WS_EX_TRANSPARENT, _WS_EX_LAYERED = 0x00000020, 0x00080000
_GWL_STYLE, _GWL_EXSTYLE = -16, -20
_GW_HWNDNEXT = 2
_DWMWA_CLOAKED = 14
_MENUS = {"#32768"}
_BARRAS = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
_AREA_DE_TRABALHO = {"Progman"}
# Janelas de fundo lidas além da em foco: mais que isso só custa tempo.
_MAX_JANELAS_DE_FUNDO = 4

# (hwnd, classe, título, estilo, pid, (x, y, largura, altura)), de cima para baixo.
Janela = Tuple[int, str, str, int, int, Tuple[int, int, int, int]]

# O COM é por thread: cada thread que lê tem a sua instância.
_local = threading.local()


def _uia():
    auto = getattr(_local, "auto", None)
    if auto is not None:
        return auto
    try:
        import comtypes
        import comtypes.client
    except ImportError as exc:
        raise ErroDoComputador("sem UI Automation: falta o pacote `comtypes`") from exc
    try:
        comtypes.CoInitialize()
    except OSError:
        pass  # a thread já iniciou o COM
    modulo = comtypes.client.GetModule("UIAutomationCore.dll")
    auto = comtypes.client.CreateObject(_CLSID_CUIAUTOMATION, interface=modulo.IUIAutomation)
    _local.auto, _local.modulo = auto, modulo
    return auto


def _cache(auto):
    pedido = auto.CreateCacheRequest()
    for propriedade in (_UIA_CONTROL_TYPE, _UIA_NAME, _UIA_BOUNDING_RECTANGLE, _UIA_IS_ENABLED,
                        _UIA_IS_OFFSCREEN, _UIA_IS_PASSWORD, *_ESTADOS_EXTRAS):
        pedido.AddProperty(propriedade)
    return pedido


def _elemento(e, cacheado: bool) -> Optional[Elemento]:
    if cacheado:
        tipo, nome, caixa = e.CachedControlType, e.CachedName, e.CachedBoundingRectangle
        fora, ativo, senha = e.CachedIsOffscreen, e.CachedIsEnabled, e.CachedIsPassword
    else:
        tipo, nome, caixa = e.CurrentControlType, e.CurrentName, e.CurrentBoundingRectangle
        fora, ativo, senha = e.CurrentIsOffscreen, e.CurrentIsEnabled, e.CurrentIsPassword
    papel = _PAPEIS.get(int(tipo))
    if papel is None or fora:
        return None
    if papel == "campo" and senha:
        papel = "senha"
    ler = e.GetCachedPropertyValue if cacheado else e.GetCurrentPropertyValue
    valor, estado = _valor_e_estado(papel, ler)
    return Elemento(papel, str(nome or ""), int(caixa.left), int(caixa.top),
                    int(caixa.right - caixa.left), int(caixa.bottom - caixa.top), bool(ativo),
                    valor=valor, estado=estado)


def _propriedade(ler, identificador):
    """O valor simples da propriedade, ou None. Quem não tem o padrão devolve
    um objeto "não suportado", que não é texto nem número."""
    try:
        bruto = ler(identificador)
    except Exception:
        return None
    return bruto if isinstance(bruto, (str, int, bool)) else None


def _valor_e_estado(papel: str, ler) -> Tuple[str, str]:
    valor = ""
    if papel in ("campo", "lista"):
        valor = str(_propriedade(ler, _UIA_VALUE_VALUE) or "")
    estado = ""
    if papel == "caixa":
        alternancia = _propriedade(ler, _UIA_TOGGLE_STATE)
        estado = {0: "desmarcada", 1: "marcada", 2: "indefinida"}.get(alternancia, "") if alternancia is not None else ""
    elif papel in ("opcao", "item", "aba"):
        estado = "selecionada" if _propriedade(ler, _UIA_SELECTION_ITEM_IS_SELECTED) is True else ""
    if papel in ("lista", "menu", "item") and not estado:
        expansao = _propriedade(ler, _UIA_EXPAND_COLLAPSE_STATE)
        estado = {0: "fechada", 1: "aberta"}.get(expansao, "") if expansao is not None else ""
    return valor, estado


def _pilha() -> List[Janela]:
    """Lista janelas visíveis pela ordem de sobreposição.

    Ignora camadas transparentes a cliques, janelas minimizadas e ocultas.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    # HWND inteiro: com o `int` padrão do ctypes, o identificador seria cortado no 64 bits.
    user32.GetTopWindow.restype = wintypes.HWND
    user32.GetTopWindow.argtypes = [wintypes.HWND]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    try:
        dwm = ctypes.windll.dwmapi
    except OSError:
        dwm = None
    saida: List[Janela] = []
    hwnd = user32.GetTopWindow(None)
    for _ in range(4000):
        if not hwnd:
            break
        atual, hwnd = hwnd, user32.GetWindow(hwnd, _GW_HWNDNEXT)
        if not user32.IsWindowVisible(atual) or user32.IsIconic(atual):
            continue
        if dwm is not None:
            escondida = ctypes.c_int(0)
            if dwm.DwmGetWindowAttribute(atual, _DWMWA_CLOAKED, ctypes.byref(escondida),
                                         ctypes.sizeof(escondida)) == 0 and escondida.value:
                continue
        estilo_ex = user32.GetWindowLongW(atual, _GWL_EXSTYLE) & 0xFFFFFFFF
        if estilo_ex & _WS_EX_TRANSPARENT and estilo_ex & _WS_EX_LAYERED:
            continue
        caixa = wintypes.RECT()
        user32.GetWindowRect(atual, ctypes.byref(caixa))
        if caixa.right <= caixa.left or caixa.bottom <= caixa.top:
            continue
        classe = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(atual, classe, 256)
        tamanho = user32.GetWindowTextLengthW(atual)
        titulo = ctypes.create_unicode_buffer(tamanho + 1)
        user32.GetWindowTextW(atual, titulo, tamanho + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(atual, ctypes.byref(pid))
        saida.append((int(atual), classe.value, titulo.value, user32.GetWindowLongW(atual, _GWL_STYLE) & 0xFFFFFFFF,
                      int(pid.value), (caixa.left, caixa.top, caixa.right - caixa.left, caixa.bottom - caixa.top)))
    return saida


def camadas_da_pilha(pilha: List[Janela], frente: int) -> List[Tuple[str, str, int, Tuple[int, int, int, int], int]]:
    """(tipo, nome, hwnd, caixa, z) do que vale ler. Sem chamada ao Windows:
    testável em qualquer sistema."""
    z_frente = next((z for z, j in enumerate(pilha) if j[0] == frente), None)
    pid_frente = pilha[z_frente][4] if z_frente is not None else None
    saida = []
    de_fundo = 0
    for z, (hwnd, classe, titulo, estilo, pid, caixa) in enumerate(pilha):
        popup_do_foco = (z_frente is not None and z < z_frente and pid == pid_frente
                         and bool(estilo & _WS_POPUP) and not titulo)
        if classe in _MENUS or popup_do_foco:
            saida.append(("menu", "menu", hwnd, caixa, z))
        elif classe in _BARRAS:
            saida.append(("barra", "barra de tarefas", hwnd, caixa, z))
        elif hwnd == frente:
            saida.append(("foco", titulo or "janela em foco", hwnd, caixa, z))
        elif classe in _AREA_DE_TRABALHO:
            saida.append(("janela", "área de trabalho", hwnd, caixa, z))
        elif titulo and de_fundo < _MAX_JANELAS_DE_FUNDO:
            de_fundo += 1
            saida.append(("janela", titulo, hwnd, caixa, z))
    return saida


def _ler_janela(auto, hwnd: int) -> List[Elemento]:
    condicao = auto.CreatePropertyCondition(_UIA_IS_CONTROL_ELEMENT, True)
    achados = auto.ElementFromHandle(hwnd).FindAllBuildCache(_TREE_SCOPE_DESCENDANTS, condicao, _cache(auto))
    saida: List[Elemento] = []
    for i in range(achados.Length):
        try:
            elemento = _elemento(achados.GetElement(i), cacheado=True)
        except Exception:
            logger.debug("Elemento UIA ilegível.", exc_info=True)
            continue
        if elemento is not None:
            saida.append(elemento)
    return saida


_TEXTO, _DOCUMENTO, _EDIT, _IMAGEM = 50020, 50030, 50004, 50006
_TEXT_PATTERN = 10014
MAX_TEXTO = 12000


def texto_da_janela(maximo: int = MAX_TEXTO) -> str:
    """Texto da janela da frente na ordem da árvore de acessibilidade.

    Inclui rótulos, parágrafos, conteúdo de documento e nome de imagens.
    Campos de senha ficam de fora.
    """
    import ctypes

    auto = _uia()
    frente = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    if not frente:
        raise ErroDoComputador("nenhuma janela na frente")
    condicao = auto.CreateOrCondition(
        auto.CreateOrCondition(auto.CreatePropertyCondition(_UIA_CONTROL_TYPE, _TEXTO),
                               auto.CreatePropertyCondition(_UIA_CONTROL_TYPE, _IMAGEM)),
        auto.CreateOrCondition(auto.CreatePropertyCondition(_UIA_CONTROL_TYPE, _DOCUMENTO),
                               auto.CreatePropertyCondition(_UIA_CONTROL_TYPE, _EDIT)))
    achados = auto.ElementFromHandle(frente).FindAll(_TREE_SCOPE_DESCENDANTS, condicao)
    partes: List[str] = []
    total = 0
    for i in range(achados.Length):
        if total >= maximo:
            break
        elemento = achados.GetElement(i)
        try:
            if elemento.CurrentIsPassword:
                continue
            tipo = elemento.CurrentControlType
            texto = ""
            if tipo in (_DOCUMENTO, _EDIT):
                try:
                    padrao = _padrao(elemento, _TEXT_PATTERN, "IUIAutomationTextPattern")
                    texto = str(padrao.DocumentRange.GetText(maximo) or "") if padrao is not None else ""
                except Exception:
                    texto = ""
                if not texto:
                    valor = elemento.GetCurrentPropertyValue(_UIA_VALUE_VALUE)
                    texto = valor if isinstance(valor, str) else ""
            else:
                texto = str(elemento.CurrentName or "")
        except Exception:
            logger.debug("Texto ilegível pela UIA.", exc_info=True)
            continue
        # Documentos do Windows separam linhas com CR; U+FFFC marca objeto embutido.
        texto = texto.replace("\r\n", "\n").replace("\r", "\n").replace("\ufffc", "").strip()
        if texto and (not partes or partes[-1] != texto):
            partes.append(texto)
            total += len(texto) + 1
    return "\n".join(partes)[:maximo]


def elementos() -> List[Elemento]:
    """Os elementos de tudo que aparece: menu aberto, janela em foco, barra de
    tarefas e as de fundo, sem o que uma cobre da outra."""
    import ctypes

    auto = _uia()
    frente = int(ctypes.windll.user32.GetForegroundWindow() or 0)
    camadas = []
    for tipo, nome, hwnd, (x, y, w, h), z in camadas_da_pilha(_pilha(), frente):
        try:
            lidos = _ler_janela(auto, hwnd)
        except Exception:
            logger.debug("Janela ilegível pela UIA: %s", nome, exc_info=True)
            continue
        camadas.append(Camada(tipo, nome, x, y, w, h, lidos, z))
    if not camadas:
        raise ErroDoComputador("a UI Automation não leu nenhuma janela")
    return juntar(camadas)


# Os padrões da UI Automation que agem no controle sem o cursor, na ordem em
# que se tentam para "apertar": botão, caixa, item, lista, e a ação padrão.
_INVOKE, _SELECTION_ITEM, _VALUE, _EXPAND_COLLAPSE, _TOGGLE, _LEGACY = 10000, 10010, 10002, 10005, 10015, 10018


def _padrao(elemento, identificador: int, interface: str):
    bruto = elemento.GetCurrentPattern(identificador)
    if not bruto:
        return None
    return bruto.QueryInterface(getattr(_local.modulo, interface))


def _achar(auto, alvo: Elemento):
    """O elemento vivo na posição do lido, subindo até o de mesmo nome e papel.
    Se a tela mudou e ele não está mais ali, erro: melhor ler de novo do que
    apertar outro."""
    e = auto.ElementFromPoint(_local.modulo.tagPOINT(*alvo.centro))
    caminhante = auto.ControlViewWalker
    for _ in range(6):
        if not e:
            break
        try:
            papel = _PAPEIS.get(int(e.CurrentControlType))
            if papel == "campo" and e.CurrentIsPassword:
                papel = "senha"
            if str(e.CurrentName or "") == alvo.nome and papel == alvo.papel:
                return e
        except Exception:
            pass
        e = caminhante.GetParentElement(e)
    raise ErroDoComputador(f"«{alvo.nome}» não está mais nesse lugar da tela; leia os elementos de novo")


def invocar(alvo: Elemento) -> str:
    """Aperta o elemento pelo padrão que ele oferece, sem mover o mouse nem
    trazer a janela para a frente. Devolve o que foi feito."""
    auto = _uia()
    e = _achar(auto, alvo)
    tentativas = (
        (_INVOKE, "IUIAutomationInvokePattern", lambda p: p.Invoke(), "apertado"),
        (_TOGGLE, "IUIAutomationTogglePattern", lambda p: p.Toggle(), "alternado"),
        (_SELECTION_ITEM, "IUIAutomationSelectionItemPattern", lambda p: p.Select(), "selecionado"),
        (_EXPAND_COLLAPSE, "IUIAutomationExpandCollapsePattern",
         lambda p: p.Collapse() if p.CurrentExpandCollapseState == 1 else p.Expand(), "aberto ou fechado"),
        (_LEGACY, "IUIAutomationLegacyIAccessiblePattern", lambda p: p.DoDefaultAction(), "ação padrão"),
    )
    for identificador, interface, agir, feito in tentativas:
        padrao = _padrao(e, identificador, interface)
        if padrao is not None:
            agir(padrao)
            return feito
    raise ErroDoComputador("esse elemento não aceita ação pela acessibilidade; use clicar nele")


def escolher_opcao(alvo: Elemento, opcao: str) -> None:
    """Escolhe a opção da lista pelo nome, sem abrir o menu. Campo de texto
    não passa por aqui: texto é digitado (`focar` +
    `digitar`), e nem a lista editável recebe texto pelo `SetValue`."""
    auto = _uia()
    e = _achar(auto, alvo)
    item = e.FindFirst(_TREE_SCOPE_DESCENDANTS, auto.CreatePropertyCondition(_UIA_NAME, opcao))
    selecao = _padrao(item, _SELECTION_ITEM, "IUIAutomationSelectionItemPattern") if item else None
    if selecao is None:
        raise ErroDoComputador(f"a lista não mostra a opção «{opcao}»; abra com invocar e escolha o item")
    selecao.Select()


def focar(alvo: Elemento) -> None:
    """O foco do teclado no elemento, sem mover o mouse. O `SetFocus` da UIA
    traz a janela dele para a frente."""
    auto = _uia()
    e = _achar(auto, alvo)
    try:
        e.SetFocus()
    except Exception as exc:
        raise ErroDoComputador("o app não deixou pôr o foco nesse elemento; clique nele e use digitar") from exc


def foco() -> Optional[Elemento]:
    auto = _uia()
    try:
        return _elemento(auto.GetFocusedElement(), cacheado=False)
    except Exception:
        logger.debug("Sem elemento em foco pela UIA.", exc_info=True)
        return None


__all__ = ["camadas_da_pilha", "elementos", "escolher_opcao", "focar", "foco", "invocar"]
