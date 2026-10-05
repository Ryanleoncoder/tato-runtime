"""Lê elementos da tela no Linux pelo AT-SPI.

Usa PyGObject e o barramento de acessibilidade para obter controles e posições.
Sem esses recursos, o controle por coordenadas da imagem permanece disponível.
Organiza menus, janela em foco e demais janelas em camadas.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from .computador_base import ErroDoComputador
from .computador_elementos import Camada, Elemento, juntar

logger = logging.getLogger(__name__)

# Para não travar numa árvore enorme (uma planilha tem milhares de células).
_MAX_NOS = 4000
_MAX_PROFUNDIDADE = 40


def _atspi():
    try:
        import gi

        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
    except (ImportError, ValueError) as exc:
        raise ErroDoComputador(f"sem AT-SPI neste Linux (python3-gi e gir1.2-atspi-2.0): {exc}") from exc
    return Atspi


def _papeis(Atspi) -> dict:
    R = Atspi.Role
    pares = {
        "PUSH_BUTTON": "botao", "TOGGLE_BUTTON": "botao", "BUTTON": "botao",
        "ENTRY": "campo", "TEXT": "campo", "SPIN_BUTTON": "campo", "PASSWORD_TEXT": "senha",
        "LINK": "link", "CHECK_BOX": "caixa", "CHECK_MENU_ITEM": "caixa",
        "RADIO_BUTTON": "opcao", "RADIO_MENU_ITEM": "opcao", "COMBO_BOX": "lista",
        "LIST_ITEM": "item", "TREE_ITEM": "item", "MENU_ITEM": "menu", "MENU": "menu",
        "PAGE_TAB": "aba", "SLIDER": "controle", "SCROLL_BAR": "controle",
    }
    return {getattr(R, nome): papel for nome, papel in pares.items() if hasattr(R, nome)}


# Os painéis dos desktops Linux: a "barra de tarefas" de cada um.
_PAINEIS = {"xfce4-panel", "gnome-shell", "plasmashell", "mate-panel", "lxpanel", "tint2", "budgie-panel",
            "cinnamon", "lxqt-panel", "wf-panel"}


def _caixa(Atspi, no) -> Tuple[int, int, int, int]:
    e = no.get_component_iface().get_extents(Atspi.CoordType.SCREEN)
    return e.x, e.y, e.width, e.height


def _janelas(Atspi) -> List[Tuple[str, str, object, Tuple[int, int, int, int]]]:
    """(tipo, nome, nó, caixa) de cada janela que aparece. O AT-SPI não diz a
    ordem da pilha; o tipo diz quem fica por cima (computador_elementos.juntar)."""
    desktop = Atspi.get_desktop(0)
    achadas = []
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is None:
            continue
        painel = (app.get_name() or "").lower() in _PAINEIS
        for j in range(app.get_child_count()):
            janela = app.get_child_at_index(j)
            if janela is None:
                continue
            try:
                estados = janela.get_state_set()
                if not estados.contains(Atspi.StateType.SHOWING):
                    continue
                papel, nome, caixa = janela.get_role(), janela.get_name() or "", _caixa(Atspi, janela)
            except Exception:
                continue
            # Menu e lista que abrem são janelas sem título (papel WINDOW) ou de menu.
            if papel in (Atspi.Role.MENU, Atspi.Role.POPUP_MENU) or (papel == Atspi.Role.WINDOW and not nome):
                achadas.append(("menu", "menu", janela, caixa))
            elif painel:
                achadas.append(("barra", "barra de tarefas", janela, caixa))
            else:
                ativa = estados.contains(Atspi.StateType.ACTIVE)
                achadas.append(("foco" if ativa else "janela", nome, janela, caixa))
    if not any(t == "foco" for t, *_ in achadas):
        # Sem gerenciador de janelas (ou com um menu aberto roubando o ACTIVE),
        # a em foco é a que contém o menu; sem menu, a primeira que aparece.
        menus = [c for t, _n, _j, c in achadas if t == "menu"]
        for k, (tipo, nome, janela, caixa) in enumerate(achadas):
            if tipo != "janela":
                continue
            x, y, w, h = caixa
            if not menus or any(x <= mx < x + w and y <= my < y + h for mx, my, _w, _h in menus):
                achadas[k] = ("foco", nome, janela, caixa)
                break
    return achadas


def _percorrer(Atspi, raiz):
    pilha = [(raiz, 0)]
    vistos = 0
    while pilha and vistos < _MAX_NOS:
        no, profundidade = pilha.pop()
        vistos += 1
        yield no
        if profundidade >= _MAX_PROFUNDIDADE:
            continue
        try:
            filhos = [no.get_child_at_index(k) for k in range(no.get_child_count())]
        except Exception:
            continue
        pilha.extend((f, profundidade + 1) for f in reversed(filhos) if f is not None)


def _elemento(Atspi, no, papeis) -> Optional[Elemento]:
    try:
        papel = papeis.get(no.get_role())
        if papel is None:
            return None
        estados = no.get_state_set()
        if not (estados.contains(Atspi.StateType.SHOWING) and estados.contains(Atspi.StateType.VISIBLE)):
            return None
        if papel == "campo" and not estados.contains(Atspi.StateType.EDITABLE) \
                and no.get_role() == Atspi.Role.TEXT:
            return None  # texto só de leitura não é campo
        caixa = no.get_component_iface().get_extents(Atspi.CoordType.SCREEN)
        valor, estado = _valor_e_estado(Atspi, no, papel, estados)
        return Elemento(papel, no.get_name() or "", caixa.x, caixa.y, caixa.width, caixa.height,
                        estados.contains(Atspi.StateType.ENABLED) or estados.contains(Atspi.StateType.SENSITIVE),
                        valor=valor, estado=estado)
    except Exception:
        logger.debug("Elemento AT-SPI ilegível.", exc_info=True)
        return None


def _valor_e_estado(Atspi, no, papel: str, estados) -> Tuple[str, str]:
    valor = ""
    if papel == "campo":
        # No PyGObject as interfaces do AT-SPI são funções da classe sobre o nó.
        try:
            valor = Atspi.Text.get_text(no, 0, min(Atspi.Text.get_character_count(no), 200))
        except Exception:
            valor = ""
    elif papel == "lista":
        # O valor da lista é a opção escolhida.
        try:
            escolhida = Atspi.Selection.get_selected_child(no, 0) if Atspi.Selection.get_n_selected_children(no) else None
            valor = (escolhida.get_name() or "") if escolhida is not None else ""
        except Exception:
            valor = ""
    estado = ""
    S = Atspi.StateType
    if papel in ("caixa", "opcao"):
        # O GTK3 não põe CHECKABLE: a caixa é marcada ou não.
        estado = "marcada" if estados.contains(S.CHECKED) else "desmarcada"
    elif papel in ("item", "aba") and estados.contains(S.SELECTED):
        estado = "selecionada"
    elif papel in ("lista", "menu") and estados.contains(S.EXPANDABLE):
        estado = "aberta" if estados.contains(S.EXPANDED) else "fechada"
    return valor or "", estado


def elementos() -> List[Elemento]:
    """Os elementos de tudo que aparece: menu aberto, janela em foco, painel e
    as outras janelas, sem o que uma cobre da outra."""
    Atspi = _atspi()
    janelas = _janelas(Atspi)
    if not janelas:
        raise ErroDoComputador("o AT-SPI não mostra nenhuma janela: o app não expõe acessibilidade")
    papeis = _papeis(Atspi)
    camadas = []
    for tipo, nome, janela, (x, y, w, h) in janelas:
        lidos = [e for no in _percorrer(Atspi, janela) if (e := _elemento(Atspi, no, papeis)) is not None]
        camadas.append(Camada(tipo, nome, x, y, w, h, lidos))
    return juntar(camadas)


# O nome da ação que "aperta", na ordem de preferência; sem nenhum, a primeira.
_ACOES_DE_APERTAR = ("click", "press", "activate", "toggle", "jump", "open")


def _achar(Atspi, alvo: Elemento):
    """O nó vivo com o mesmo papel e nome cujo retângulo contém o centro do lido.
    Se a tela mudou e ele não está mais ali, erro: melhor ler de novo do que
    apertar outro."""
    papeis = _papeis(Atspi)
    x, y = alvo.centro
    for _tipo, _nome, janela, (jx, jy, jw, jh) in _janelas(Atspi):
        if not (jx <= x < jx + jw and jy <= y < jy + jh):
            continue
        for no in _percorrer(Atspi, janela):
            try:
                if (no.get_name() or "") != alvo.nome:
                    continue
                papel = papeis.get(no.get_role())
                if papel != alvo.papel and not (alvo.papel == "senha" and papel == "campo"):
                    continue
                cx, cy, cw, ch = _caixa(Atspi, no)
                if cx <= x < cx + cw and cy <= y < cy + ch:
                    return no
            except Exception:
                continue
    raise ErroDoComputador(f"«{alvo.nome}» não está mais nesse lugar da tela; leia os elementos de novo")


def invocar(alvo: Elemento) -> str:
    """Aperta pela ação que o elemento oferece, sem mover o mouse."""
    Atspi = _atspi()
    no = _achar(Atspi, alvo)
    try:
        total = Atspi.Action.get_n_actions(no)
    except Exception:
        total = 0
    if not total:
        raise ErroDoComputador("esse elemento não aceita ação pela acessibilidade; use clicar nele")
    nomes = [str(Atspi.Action.get_action_name(no, i) or "").lower() for i in range(total)]
    indice = next((nomes.index(n) for n in _ACOES_DE_APERTAR if n in nomes), 0)
    if not Atspi.Action.do_action(no, indice):
        raise ErroDoComputador("o app recusou a ação; use clicar nele")
    return nomes[indice] or "ação padrão"


def lista_editavel(alvo: Elemento) -> bool:
    Atspi = _atspi()
    no = _achar(Atspi, alvo)
    return bool(no.get_state_set().contains(Atspi.StateType.EDITABLE))


def escolher_opcao(alvo: Elemento, opcao: str) -> None:
    """Escolhe a opção da lista pelo nome, direto na lista. Campo de texto não
    passa por aqui: texto é digitado (`focar` + `digitar`)."""
    Atspi = _atspi()
    no = _achar(Atspi, alvo)
    # A seleção é da própria lista; as opções moram no menu dela, e o índice
    # é o da opção dentro do menu.
    for item in _percorrer(Atspi, no):
        if item is no or (item.get_name() or "") != opcao:
            continue
        try:
            if Atspi.Selection.select_child(no, item.get_index_in_parent()):
                return
        except Exception:
            logger.debug("Seleção pela lista recusada.", exc_info=True)
        break
    raise ErroDoComputador(f"a lista não aceitou a opção «{opcao}»; abra com invocar e escolha o item")


def focar(alvo: Elemento) -> None:
    """O foco do teclado no elemento, sem mover o mouse. O GTK traz a janela
    dele para a frente junto."""
    Atspi = _atspi()
    no = _achar(Atspi, alvo)
    try:
        if Atspi.Component.grab_focus(no):
            return
    except Exception:
        logger.debug("Foco pela acessibilidade recusado.", exc_info=True)
    raise ErroDoComputador("o app não deixou pôr o foco nesse elemento; clique nele e use digitar")


def foco() -> Optional[Elemento]:
    """O elemento com o foco do teclado: no menu aberto, se houver, senão na
    janela em foco."""
    Atspi = _atspi()
    papeis = _papeis(Atspi)
    candidatas = [(t, j) for t, _n, j, _c in _janelas(Atspi) if t in ("menu", "foco")]
    for _tipo, janela in sorted(candidatas, key=lambda par: par[0] != "menu"):
        for no in _percorrer(Atspi, janela):
            try:
                if no.get_state_set().contains(Atspi.StateType.FOCUSED):
                    return _elemento(Atspi, no, papeis)
            except Exception:
                continue
    return None


__all__ = ["elementos", "escolher_opcao", "focar", "foco", "invocar"]
