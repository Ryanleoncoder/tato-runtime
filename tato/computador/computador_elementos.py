"""Lê controles e posições pela acessibilidade do sistema.

Agrupa janelas em camadas e exclui elementos encobertos por outras janelas.
Numera os controles para permitir gestos por elemento.
Quando um aplicativo não expõe controles, usa coordenadas da imagem.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Optional, Tuple

from .computador_base import Tela

# O papel é nosso, o mesmo para os dois sistemas; cada leitor traduz o dele.
PAPEIS = {
    "botao": "botão", "campo": "campo", "senha": "campo de senha", "link": "link",
    "caixa": "caixa de marcar", "opcao": "opção", "lista": "lista suspensa", "item": "item",
    "menu": "menu", "aba": "aba", "controle": "controle",
}

# Mais que isso vira parede de número em cima do print e não ajuda o modelo.
MAX_ELEMENTOS = 80
# Cada janela que não é a em foco entra com pouco: o resto é do foco e do menu.
MAX_POR_JANELA_DE_FUNDO = 15
_MENOR_LADO = 4


@dataclass(frozen=True)
class Elemento:
    """Em pixels da TELA, como os gestos."""

    papel: str
    nome: str
    x: int
    y: int
    largura: int
    altura: int
    ativo: bool = True
    # De onde veio: o título da janela, "menu" ou "barra de tarefas".
    janela: str = ""
    # O texto de um campo ou lista (nunca de senha) e o estado de caixa, item
    # e lista: "marcada", "desmarcada", "selecionada", "aberta", "fechada".
    # Com eles o veredito diz se o gesto fez efeito sem print.
    valor: str = ""
    estado: str = ""

    @property
    def centro(self) -> Tuple[int, int]:
        return self.x + self.largura // 2, self.y + self.altura // 2


# Quanto menor, mais importa para os números. E, sem ordem de empilhamento
# conhecida, também quem fica por cima: menu sobre tudo, barra sobre as janelas.
_PRIORIDADE = {"menu": 0, "foco": 1, "barra": 2, "janela": 3}
_EMPILHAMENTO = {"menu": 0, "barra": 1, "foco": 2, "janela": 3}


@dataclass
class Camada:
    """Uma janela lida pela acessibilidade. `z` é a posição na pilha (0 é a de
    cima) quando o sistema diz; sem ele, vale o tipo."""

    tipo: str  # menu, foco, barra ou janela
    nome: str
    x: int
    y: int
    largura: int
    altura: int
    elementos: List[Elemento]
    z: Optional[int] = None

    def contem(self, ponto: Tuple[int, int]) -> bool:
        return self.x <= ponto[0] < self.x + self.largura and self.y <= ponto[1] < self.y + self.altura


def _por_cima(de: Camada, sobre: Camada) -> bool:
    """`de` cobre `sobre`? Com a pilha conhecida dos dois, ela manda. Sem ela,
    o tipo; e duas janelas de fundo sem ordem se cobrem uma à outra, que é o
    lado seguro: some o que pode estar escondido."""
    if de is sobre:
        return False
    if de.z is not None and sobre.z is not None:
        return de.z < sobre.z
    if de.tipo == sobre.tipo == "janela":
        return True
    return _EMPILHAMENTO[de.tipo] < _EMPILHAMENTO[sobre.tipo]


def juntar(camadas: List[Camada]) -> List[Elemento]:
    """As camadas numa lista só: tira o que outra cobre, marca de onde veio e
    põe na ordem dos números."""
    ordem = sorted(camadas, key=lambda c: (_PRIORIDADE[c.tipo], c.z if c.z is not None else 0))
    saida: List[Elemento] = []
    for camada in ordem:
        acima = [c for c in camadas if _por_cima(c, camada)]
        visiveis = [replace(e, janela=camada.nome) for e in camada.elementos
                    if not any(c.contem(e.centro) for c in acima)]
        if camada.tipo == "janela":
            visiveis = _em_ordem_de_leitura(visiveis)[:MAX_POR_JANELA_DE_FUNDO]
        saida.extend(visiveis)
    return saida


def _em_ordem_de_leitura(elementos: List[Elemento]) -> List[Elemento]:
    # O espaço entre colunas separa regiões; uma barra de botões continua numa linha.
    faixas = sorted((e.x, e.x + e.largura) for e in elementos)
    direita = faixas[0][1] if faixas else 0
    for inicio, fim in faixas[1:]:
        if inicio - direita >= 48:
            esquerda = [e for e in elementos if e.x < inicio]
            restante = [e for e in elementos if e.x >= inicio]
            if (any(len({e.y // 12 for e in esquerda if e.x // 48 == coluna}) >= 2
                    for coluna in {e.x // 48 for e in esquerda})
                    and any(len({e.y // 12 for e in restante if e.x // 48 == coluna}) >= 2
                            for coluna in {e.x // 48 for e in restante})
                    and max(min(e.y for e in esquerda), min(e.y for e in restante))
                    < min(max(e.y + e.altura for e in esquerda),
                          max(e.y + e.altura for e in restante))):
                return _em_ordem_de_leitura(esquerda) + _em_ordem_de_leitura(restante)
        direita = max(direita, fim)
    # Linhas de 12 px: dois botões lado a lado na mesma barra ficam em ordem de leitura.
    return sorted(elementos, key=lambda e: (e.y // 12, e.x))


def filtrar(elementos: Iterable[Elemento], tamanho: Tuple[int, int]) -> List[Elemento]:
    """Só o que aparece na tela, sem repetição, em ordem de leitura dentro de
    cada janela (as janelas ficam na ordem em que vieram), até `MAX_ELEMENTOS`."""
    largura, altura = tamanho
    vistos = set()
    saida: List[Elemento] = []
    for e in elementos:
        if e.papel not in PAPEIS or e.largura < _MENOR_LADO or e.altura < _MENOR_LADO:
            continue
        if e.papel == "menu" and not e.nome.strip():
            continue  # o contêiner do menu aberto, não um item dele
        x0, y0 = max(0, e.x), max(0, e.y)
        x1, y1 = min(largura, e.x + e.largura), min(altura, e.y + e.altura)
        if x1 - x0 < _MENOR_LADO or y1 - y0 < _MENOR_LADO:
            continue
        valor = "" if e.papel == "senha" else " ".join(e.valor.split())[:80]
        cortado = Elemento(e.papel, " ".join(e.nome.split())[:80], x0, y0, x1 - x0, y1 - y0, e.ativo, e.janela,
                           valor, e.estado)
        chave = (cortado.x, cortado.y, cortado.largura, cortado.altura)
        if chave in vistos:
            continue
        vistos.add(chave)
        saida.append(cortado)
    saida = _sem_duplicata_de_nome(saida)
    grupos: Dict[str, List[Elemento]] = {}
    for e in saida:
        grupos.setdefault(e.janela, []).append(e)
    ordenados = [e for grupo in grupos.values() for e in _em_ordem_de_leitura(grupo)]
    if len(ordenados) > MAX_ELEMENTOS:
        # A casca do navegador (dezenas de abas) vinha antes do conteúdo e gastava o teto:
        # as abas que não estão selecionadas saem antes de cortar a página.
        ordenados = [e for e in ordenados if e.papel != "aba" or e.estado == "selecionada"]
    return ordenados[:MAX_ELEMENTOS]


# O que dá para acionar vale mais que o contêiner com o mesmo nome no mesmo lugar.
_ACIONAVEL = {"link": 0, "botao": 0, "campo": 0, "caixa": 0, "opcao": 1, "aba": 1, "menu": 1, "lista": 1,
              "controle": 2, "item": 3}
_PERTO = 12


def _sem_duplicata_de_nome(elementos: List[Elemento]) -> List[Elemento]:
    """Item e link com o mesmo nome no mesmo lugar (a lista da página embrulha o
    link): fica só o acionável."""
    saida: List[Elemento] = []
    for e in elementos:
        igual = next((i for i, o in enumerate(saida) if o.nome and o.nome == e.nome and o.janela == e.janela
                      and abs(o.centro[0] - e.centro[0]) <= _PERTO and abs(o.centro[1] - e.centro[1]) <= _PERTO), None)
        if igual is None:
            saida.append(e)
        elif _ACIONAVEL.get(e.papel, 2) < _ACIONAVEL.get(saida[igual].papel, 2):
            saida[igual] = e
    return saida


def para_o_modelo(elementos: List[Elemento], escala: float) -> List[Dict[str, object]]:
    """A lista para o modelo: número, papel, nome e o centro no espaço do print
    (`escala` é tela → print, a mesma do `computador.escala`)."""
    varias = len({e.janela for e in elementos}) > 1
    lista = []
    for n, e in enumerate(elementos, 1):
        cx, cy = e.centro
        item: Dict[str, object] = {"n": n, "papel": PAPEIS[e.papel], "nome": e.nome,
                                   "x": round(cx * escala), "y": round(cy * escala)}
        if varias and e.janela:
            item["janela"] = e.janela
        if e.valor:
            item["valor"] = e.valor
        if e.estado:
            item["estado"] = e.estado
        if not e.ativo:
            item["desativado"] = True
        lista.append(item)
    return lista


def desenhar(tela: Tela, elementos: List[Elemento]) -> Tela:
    """O print com uma caixa e o número de cada elemento, no espaço do print."""
    from PIL import Image, ImageDraw, ImageFont

    imagem = Image.open(io.BytesIO(tela.png)).convert("RGB")
    pincel = ImageDraw.Draw(imagem)
    fx = tela.largura / max(1, tela.largura_real)
    fy = tela.altura / max(1, tela.altura_real)
    try:
        fonte = ImageFont.load_default(size=12)
    except TypeError:  # Pillow antigo: sem tamanho
        fonte = ImageFont.load_default()
    for n, e in enumerate(elementos, 1):
        x0, y0 = round(e.x * fx), round(e.y * fy)
        x1, y1 = round((e.x + e.largura) * fx), round((e.y + e.altura) * fy)
        pincel.rectangle((x0, y0, x1, y1), outline=(245, 196, 0), width=2)
        rotulo = str(n)
        caixa = pincel.textbbox((0, 0), rotulo, font=fonte)
        lx, ly = x0, max(0, y0 - (caixa[3] - caixa[1]) - 4)
        pincel.rectangle((lx, ly, lx + caixa[2] - caixa[0] + 6, ly + caixa[3] - caixa[1] + 4), fill=(24, 24, 27))
        pincel.text((lx + 3 - caixa[0], ly + 2 - caixa[1]), rotulo, fill=(245, 196, 0), font=fonte)
    saida = io.BytesIO()
    imagem.save(saida, format="PNG", optimize=True)
    return Tela(saida.getvalue(), tela.largura, tela.altura, tela.largura_real, tela.altura_real)


__all__ = ["Camada", "Elemento", "MAX_ELEMENTOS", "PAPEIS", "desenhar", "filtrar", "juntar", "para_o_modelo"]
