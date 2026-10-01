"""Navegador determinístico para testes, alimentado por um HTML do disco.

Não abre processo, não toca a rede e não tenta simular um browser inteiro. Ele
exercita o contrato que importa ao Harness: refs estáveis, estado preservado
entre ações e uma nova observação depois de cada mutação.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from threading import RLock
from typing import Any

from .browser_contracts import Action, BrowserCapability, Observation
from .browser_provider import BrowserProvider


_INTERATIVOS = {"a", "button", "input", "select", "textarea"}
_VISIVEIS = _INTERATIVOS | {"h1", "h2", "h3", "p", "li"}
_PAPEIS = {
    "a": "link",
    "button": "button",
    "input": "textbox",
    "select": "combobox",
    "textarea": "textbox",
    "h1": "heading",
    "h2": "heading",
    "h3": "heading",
    "p": "paragraph",
    "li": "listitem",
}


@dataclass
class _Elemento:
    tag: str
    atributos: dict[str, str]
    ordem: int
    escondido_por: str | None = None
    texto: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str | None:
        return f"e{self.ordem}" if self.tag in _INTERATIVOS else None

    @property
    def nome(self) -> str:
        texto = " ".join(" ".join(self.texto).split())
        return self.atributos.get("aria-label") or texto or self.atributos.get("value", "")


class _LeitorHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.titulo = ""
        self.elementos: list[_Elemento] = []
        self._pilha: list[tuple[str, int | None, str | None]] = []
        self._interativos = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        atributos = {nome: valor or "" for nome, valor in attrs}
        escondido_herdado = self._pilha[-1][2] if self._pilha else None
        escondido = escondido_herdado
        if "hidden" in atributos or "display:none" in atributos.get("style", "").replace(" ", "").lower():
            escondido = atributos.get("id") or escondido_herdado or f"__hidden_{len(self._pilha)}"

        indice: int | None = None
        if tag in _VISIVEIS or tag == "title":
            if tag in _INTERATIVOS:
                self._interativos += 1
                ordem = self._interativos
            else:
                ordem = 0
            self.elementos.append(_Elemento(tag, atributos, ordem, escondido))
            indice = len(self.elementos) - 1
        self._pilha.append((tag, indice, escondido))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not data.strip():
            return
        for tag, indice, _ in self._pilha:
            if indice is not None:
                self.elementos[indice].texto.append(data)
            if tag == "title":
                self.titulo = " ".join(data.split())

    def handle_endtag(self, tag: str) -> None:
        for posicao in range(len(self._pilha) - 1, -1, -1):
            if self._pilha[posicao][0] == tag:
                del self._pilha[posicao:]
                return


class FakeBrowserSession(BrowserCapability):
    def __init__(self, session_id: str, html: str, origem: Path) -> None:
        self.session_id = session_id
        self._origem = origem
        self._epoch = 0
        self._revelados: set[str] = set()
        self._valores: dict[str, str] = {}
        self._scroll_y = 0
        leitor = _LeitorHTML()
        leitor.feed(html)
        self._titulo = leitor.titulo
        self._elementos = leitor.elementos

    def _visivel(self, elemento: _Elemento) -> bool:
        return elemento.escondido_por is None or elemento.escondido_por in self._revelados

    def _por_ref(self, ref: str) -> _Elemento:
        for elemento in self._elementos:
            if elemento.ref == ref and self._visivel(elemento):
                return elemento
        raise ValueError(f"ref de fixture desconhecida ou invisível: {ref}")

    def _snapshot(self) -> Observation:
        arvore = []
        for elemento in self._elementos:
            if elemento.tag == "title" or not self._visivel(elemento):
                continue
            item: dict[str, Any] = {
                "role": _PAPEIS[elemento.tag],
                "name": self._valores.get(elemento.ref or "", elemento.nome),
            }
            if elemento.ref:
                item["ref"] = elemento.ref
            if elemento.tag.startswith("h"):
                item["level"] = int(elemento.tag[1])
            arvore.append(item)
        return Observation(
            fonte="fixture_accessibility",
            dados={"titulo": self._titulo, "url": self._origem.as_uri(), "arvore": arvore},
            epoch=self._epoch,
            confianca=1.0,
            interativa=any("ref" in item for item in arvore),
            metadados={"fixture": str(self._origem), "scroll_y": self._scroll_y},
        )

    async def execute(self, action: Action) -> Observation:
        if action.tipo == "snapshot":
            return self._snapshot()
        if action.tipo == "clicar":
            elemento = self._por_ref(str(action.argumentos.get("ref") or ""))
            revelar = elemento.atributos.get("data-reveal")
            if revelar:
                self._revelados.add(revelar)
            self._epoch += 1
            return self._snapshot()
        if action.tipo == "digitar":
            elemento = self._por_ref(str(action.argumentos.get("ref") or ""))
            if elemento.tag not in {"input", "textarea"}:
                raise ValueError("digitar exige input ou textarea")
            self._valores[elemento.ref or ""] = str(action.argumentos.get("texto") or "")
            self._epoch += 1
            return self._snapshot()
        if action.tipo == "rolar":
            self._scroll_y += int(action.argumentos.get("delta_y") or action.argumentos.get("y") or 0)
            self._epoch += 1
            return self._snapshot()
        if action.tipo == "console":
            return Observation("fixture_console", [], self._epoch, 1.0, False)
        if action.tipo == "imagens":
            return Observation("fixture_images", [], self._epoch, 1.0, False)
        if action.tipo == "navegar":
            raise ValueError("o provedor falso nasce ligado a uma fixture; crie outra sessão para navegar")
        raise ValueError(f"ação não implementada no provedor falso: {action.tipo}")


class FakeBrowserProvider(BrowserProvider):
    def __init__(self, fixture: str | Path, *, name: str = "fixture") -> None:
        self._fixture = Path(fixture).resolve()
        self._name = name
        self._sessoes: dict[str, FakeBrowserSession] = {}
        self._lock = RLock()

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        return self._fixture.is_file()

    def create_session(self, task_id: str) -> BrowserCapability:
        if not self.is_available():
            raise FileNotFoundError(self._fixture)
        session_id = f"{self.name}:{task_id}"
        with self._lock:
            sessao = FakeBrowserSession(
                session_id,
                self._fixture.read_text(encoding="utf-8"),
                self._fixture,
            )
            self._sessoes[session_id] = sessao
            return sessao

    def close_session(self, session_id: str) -> bool:
        with self._lock:
            return self._sessoes.pop(session_id, None) is not None

    def emergency_cleanup(self, session_id: str) -> None:
        self.close_session(session_id)


__all__ = ["FakeBrowserProvider", "FakeBrowserSession"]
