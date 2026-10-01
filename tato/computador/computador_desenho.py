"""Desenha a moldura transparente sobre a tela controlada.

A borda, o aviso de uso, as falas recentes e o gesto atual ficam visíveis.
O aviso muda de posição quando o ponteiro se aproxima.
Qualquer cliente usa o mesmo gradiente, do ciano ao coral.
O backend apresenta a imagem RGBA e recebe o atalho de parada.
"""
from __future__ import annotations

import functools
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ESCURO = (0x18, 0x18, 0x1B)
CLARO = (0xFA, 0xFA, 0xF9)
CINZA_CLARO = (0xD6, 0xD3, 0xD1)
CINZA = (0xA1, 0xA1, 0xAA)
BORDA_DA_TECLA = (0x52, 0x52, 0x5B)

Cores = Tuple[Tuple[int, int, int], ...]
# Gradiente da borda, do balão, da onda e da caixinha.
PALETA: Cores = ((0x36, 0xE2, 0xF5), (0x2F, 0x5B, 0xFF), (0x8B, 0x3D, 0xFF), (0xE2, 0x4B, 0xD8),
                 (0xFF, 0x7A, 0x59))

BORDA = 4
BRILHO = 26
HUD_LARGURA = 300
HUD_MARGEM = 24

_FONTES = {
    "normal": ("segoeui.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf", "FreeSans.ttf"),
    "forte": ("seguisb.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "FreeSansBold.ttf"),
    "mono": ("consola.ttf", "DejaVuSansMono.ttf", "LiberationMono-Regular.ttf", "FreeMono.ttf"),
}
_PASTAS = ("C:/Windows/Fonts", "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/liberation",
           "/usr/share/fonts/truetype/freefont", "/usr/share/fonts/TTF", "/System/Library/Fonts/Supplemental")


@functools.lru_cache(maxsize=32)
def fonte(peso: str, tamanho: int) -> ImageFont.ImageFont:
    """A primeira que existir: Segoe UI no Windows, DejaVu no Linux."""
    import os

    for nome in _FONTES.get(peso, _FONTES["normal"]):
        for pasta in _PASTAS:
            caminho = os.path.join(pasta, nome)
            if os.path.exists(caminho):
                return ImageFont.truetype(caminho, tamanho)
    return ImageFont.load_default(tamanho)


@dataclass(frozen=True)
class Regiao:
    """Retângulo com moldura dentro: o X11 põe uma janela em cada um."""

    nome: str
    x: int
    y: int
    largura: int
    altura: int


def _largura(texto: str, f) -> int:
    return int(f.getlength(texto))


def _quebrar(texto: str, f, largura: int, linhas: int = 2) -> List[str]:
    palavras, saida, atual = texto.split(), [], ""
    for palavra in palavras:
        tentativa = f"{atual} {palavra}".strip()
        if _largura(tentativa, f) <= largura or not atual:
            atual = tentativa
            continue
        saida.append(atual)
        atual = palavra
        if len(saida) == linhas:
            break
    if atual and len(saida) < linhas:
        saida.append(atual)
    if len(saida) == linhas and " ".join(saida) != texto.strip():
        while saida[-1] and _largura(saida[-1] + "…", f) > largura:
            saida[-1] = saida[-1][:-1]
        saida[-1] = saida[-1].rstrip() + "…"
    return saida


@functools.lru_cache(maxsize=32)
def _degrade(cores: Cores, largura: int, altura: int) -> Image.Image:
    if len(cores) == 1:
        return Image.new("RGB", (largura, altura), cores[0])
    faixa = Image.new("RGB", (len(cores), 1))
    faixa.putdata(list(cores))
    return faixa.resize((largura, 1), Image.BILINEAR).resize((largura, altura), Image.NEAREST)


def _pintar(camada: Image.Image, caixa, cores: Cores, forma) -> None:
    """`forma(d, caixa)` desenha a máscara em tons de cinza; a paleta entra
    onde ela tem alfa. O que sai da tela é cortado."""
    w, h = camada.size
    x0, y0 = max(0, math.floor(caixa[0])), max(0, math.floor(caixa[1]))
    x1, y1 = min(w, math.ceil(caixa[2]) + 1), min(h, math.ceil(caixa[3]) + 1)
    if x1 <= x0 or y1 <= y0:
        return
    mascara = Image.new("L", (x1 - x0, y1 - y0), 0)
    forma(ImageDraw.Draw(mascara), (caixa[0] - x0, caixa[1] - y0, caixa[2] - x0, caixa[3] - y0))
    tinta = _degrade(cores, x1 - x0, y1 - y0).convert("RGBA")
    tinta.putalpha(mascara)
    camada.alpha_composite(tinta, (x0, y0))


def _ponto(camada: Image.Image, caixa, cores: Cores) -> None:
    _pintar(camada, caixa, cores, lambda d, c: d.ellipse(c, fill=255))


def _borda(camada: Image.Image, brilho: bool = True, cores: Cores = PALETA) -> None:
    """4 px da cor, e o brilho que entra na tela e some."""
    w, h = camada.size

    def forma(d, _caixa):
        for passo in range(BRILHO if brilho else 0):
            alfa = round(0.35 * 255 * (1 - passo / BRILHO) ** 2)
            d.rectangle((passo, passo, w - 1 - passo, h - 1 - passo), outline=alfa)
        for passo in range(BORDA):
            d.rectangle((passo, passo, w - 1 - passo, h - 1 - passo), outline=255)

    _pintar(camada, (0, 0, w - 1, h - 1), cores, forma)


_ALTURA_DA_PILULA = 26
_MARGEM_DA_PILULA = 10


def _largura_da_pilula(atalho: str, agente: str) -> int:
    normal, mono = fonte("forte", 12), fonte("mono", 11)
    return 12 + 7 + 8 + _largura(f"{agente} está usando o computador", normal) + 8 + _largura("·", normal) + 8 \
        + _largura(atalho or "Ctrl+Alt+Shift+S", mono) + 10 + 8 + _largura("para", normal) + 12


def mouse_na_pilula(mouse: Tuple[int, int], tela: Tuple[int, int], quadro) -> bool:
    """O mouse chegou perto da pílula no alto: ela desce, para não cobrir o que ele mexe."""
    largura = _largura_da_pilula(getattr(quadro, "atalho", ""), getattr(quadro, "agente", "") or "Tato")
    x = (tela[0] - largura) // 2
    return x - 40 <= mouse[0] <= x + largura + 40 and mouse[1] <= _MARGEM_DA_PILULA + _ALTURA_DA_PILULA + 40


def onde_esta_o_mouse(mouse: Tuple[int, int], tela: Tuple[int, int], quadro, rodape: int = 0) -> Tuple[bool, bool]:
    """Perto da caixinha, perto da pílula: quando muda, o backend redesenha."""
    return mouse_perto(mouse, tela, rodape=rodape), mouse_na_pilula(mouse, tela, quadro)


def _pilula(camada: Image.Image, atalho: str, agente: str = "Tato", embaixo: bool = False,
            rodape: int = 0) -> Regiao:
    w, h = camada.size
    normal, mono = fonte("forte", 12), fonte("mono", 11)
    texto = f"{agente} está usando o computador"
    tecla = atalho or "Ctrl+Alt+Shift+S"
    largura = _largura_da_pilula(atalho, agente)
    altura = _ALTURA_DA_PILULA
    y = h - rodape - altura - _MARGEM_DA_PILULA if embaixo else _MARGEM_DA_PILULA
    x = (w - largura) // 2
    d = ImageDraw.Draw(camada)
    d.rounded_rectangle((x, y, x + largura, y + altura), radius=altura // 2, fill=(*ESCURO, 255))
    cx = x + 12
    _ponto(camada, (cx, y + 9, cx + 7, y + 16), PALETA)
    cx += 7 + 8
    d.text((cx, y + altura // 2), texto, font=normal, fill=(*CLARO, 255), anchor="lm")
    cx += _largura(texto, normal) + 8
    d.text((cx, y + altura // 2), "·", font=normal, fill=(*CINZA, 255), anchor="lm")
    cx += _largura("·", normal) + 8
    lt = _largura(tecla, mono) + 10
    d.rounded_rectangle((cx, y + 5, cx + lt, y + altura - 5), radius=4, outline=(*BORDA_DA_TECLA, 255))
    d.text((cx + 5, y + altura // 2), tecla, font=mono, fill=(*CLARO, 255), anchor="lm")
    cx += lt + 8
    d.text((cx, y + altura // 2), "para", font=normal, fill=(*CINZA_CLARO, 255), anchor="lm")
    return Regiao("pilula", x, y, largura + 1, altura + 1)


def _caixinha(camada: Image.Image, contexto: str, falas, mouse: Optional[Tuple[int, int]],
              rodape: int = 0, agente: str = "Tato") -> Regiao:
    """A caixinha das mensagens: embaixo à direita, acima da barra de tarefas
    (`rodape`), e no alto quando o mouse chega perto, para não cobrir o que
    ele está mexendo."""
    w, h = camada.size
    h -= rodape
    cabeca, forte, corpo = fonte("normal", 11), fonte("forte", 11), fonte("normal", 13)
    interna = HUD_LARGURA - 28
    blocos = [(_quebrar(f.texto, corpo, interna), f.opacidade) for f in falas]
    altura = 12 + 16 + 8 + sum(len(linhas) * 18 for linhas, _ in blocos) + 5 * max(0, len(blocos) - 1) + 12
    x = w - HUD_LARGURA - HUD_MARGEM
    y = h - altura - HUD_MARGEM
    if mouse is not None and mouse_perto(mouse, (w, h), altura):
        y = 52
    d = ImageDraw.Draw(camada)
    d.rounded_rectangle((x, y, x + HUD_LARGURA, y + altura), radius=12, fill=(*ESCURO, round(0.78 * 255)))
    cy = y + 12 + 8
    _ponto(camada, (x + 14, cy - 3, x + 21, cy + 4), PALETA)
    d.text((x + 29, cy), agente, font=forte, fill=(*CLARO, 255), anchor="lm")
    if contexto:
        d.text((x + 29 + _largura(agente, forte) + 8, cy), contexto, font=cabeca,
               fill=(*CINZA_CLARO, 255), anchor="lm")
    cy = y + 12 + 16 + 8
    for linhas, opacidade in blocos:
        for linha in linhas:
            # Esmaecer misturando a cor, com o texto opaco: alfa no texto
            # SUBSTITUI o da caixinha, e a linha "apagada" mostrava a tela
            # clara de trás, ficando mais forte que a atual.
            cor = tuple(round(e + (c - e) * opacidade) for e, c in zip(ESCURO, CLARO))
            d.text((x + 14, cy), linha, font=corpo, fill=(*cor, 255))
            cy += 18
        cy += 5
    return Regiao("caixinha", x, y, HUD_LARGURA + 1, altura + 1)


def mouse_perto(mouse: Tuple[int, int], tela: Tuple[int, int], altura_da_caixinha: int = 120,
                rodape: int = 0) -> bool:
    w, h = tela
    h -= rodape
    return mouse[0] > w - HUD_LARGURA - HUD_MARGEM - 60 and mouse[1] > h - altura_da_caixinha - HUD_MARGEM - 60


def _balao(camada: Image.Image, mouse: Tuple[int, int], etiqueta: str, cores: Cores = PALETA) -> Regiao:
    w, h = camada.size
    f = fonte("forte", 12)
    largura, altura = _largura(etiqueta, f) + 16, 24
    x, y = mouse[0] + 24, mouse[1] + 22
    # Perto da borda direita ou de baixo, o balão vira para o outro lado.
    if x + largura > w - 8:
        x = mouse[0] - 16 - largura
    if y + altura > h - 8:
        y = mouse[1] - 16 - altura
    d = ImageDraw.Draw(camada)
    d.rounded_rectangle((x, y, x + largura, y + altura), radius=6, fill=(*ESCURO, 255))
    _pintar(camada, (x, y, x + largura, y + altura), cores,
            lambda d, c: d.rounded_rectangle(c, radius=6, outline=255))
    d.text((x + 8, y + altura // 2), etiqueta, font=f, fill=(*CLARO, 255), anchor="lm")
    return Regiao("balao", x, y, largura + 1, altura + 1)


def _onda(camada: Image.Image, mouse: Tuple[int, int], cores: Cores = PALETA) -> Regiao:
    raio = 20
    x, y = mouse
    _pintar(camada, (x - raio, y - raio, x + raio, y + raio), cores,
            lambda d, c: d.ellipse(c, outline=230, width=2))
    return Regiao("onda", x - raio - 1, y - raio - 1, 2 * raio + 3, 2 * raio + 3)


def desenhar(tamanho: Tuple[int, int], quadro, mouse: Optional[Tuple[int, int]] = None,
             brilho: bool = True, rodape: int = 0) -> Tuple[Image.Image, List[Regiao]]:
    """A moldura inteira, e onde ela tem pixel (para quem desenha por pedaço).

    `brilho=False` é para quem compõe sobre uma foto do que está embaixo (o X11
    sem compositor): a faixa de brilho mostraria pixels parados de um jogo que
    continua rodando. Fica só a borda sólida."""
    w, h = tamanho
    camada = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if not getattr(quadro, "aberta", False):
        return camada, []
    faixa = BRILHO if brilho else BORDA
    regioes = [Regiao("borda_cima", 0, 0, w, faixa), Regiao("borda_baixo", 0, h - faixa, w, faixa),
               Regiao("borda_esquerda", 0, faixa, faixa, h - 2 * faixa),
               Regiao("borda_direita", w - faixa, faixa, faixa, h - 2 * faixa)]
    agente = getattr(quadro, "agente", "") or "Tato"
    cores = PALETA
    _borda(camada, brilho, cores)
    embaixo = mouse is not None and mouse_na_pilula(mouse, (w, h), quadro)
    regioes.append(_pilula(camada, getattr(quadro, "atalho", ""), agente, embaixo, rodape))
    if quadro.falas:
        regioes.append(_caixinha(camada, getattr(quadro, "contexto", ""), quadro.falas, mouse, rodape, agente))
    alvo = getattr(quadro, "ponto", None) or mouse
    if alvo is not None and getattr(quadro, "clique", False):
        regioes.append(_onda(camada, alvo, cores))
    if alvo is not None and getattr(quadro, "etiqueta", ""):
        regioes.append(_balao(camada, alvo, quadro.etiqueta, cores))
    return camada, [_dentro(r, w, h) for r in regioes]


def _dentro(r: Regiao, w: int, h: int) -> Regiao:
    x, y = max(0, r.x), max(0, r.y)
    return Regiao(r.nome, x, y, max(1, min(r.largura, w - x)), max(1, min(r.altura, h - y)))


# As capturas de Windows e X11 não incluem o cursor; ele é composto na imagem.
_SETA = [(4, 2), (19, 13.5), (12.4, 14.6), (16.3, 21.5), (13.5, 23), (9.6, 16.1), (4.5, 20.5)]

# Seta mostrada enquanto o agente controla o mouse. Os vértices são do miolo;
# o contorno é alargado por `_RAIO_DO_TATO`.
_SETA_DO_TATO = [(4.8, 4.3), (20.6, 14.2), (11.8, 14.6), (8.8, 20.6)]
_RAIO_DO_TATO = 1.9
PONTA_DO_TATO = (3.5, 3.0)
_CORES_DA_SETA: Cores = ((0x2C, 0xD4, 0xF5), (0x1F, 0x6B, 0xFF), (0x2A, 0x3C, 0xF0), (0x8B, 0x3D, 0xFF),
                         (0xE0, 0x4B, 0xE8))


def _clarear(cores: Cores, quanto: float) -> Cores:
    return tuple(tuple(round(c + (255 - c) * quanto) for c in cor) for cor in cores)


@functools.lru_cache(maxsize=8)
def seta(tamanho: int = 32) -> Tuple[Image.Image, Tuple[int, int]]:
    """A seta em RGBA e o ponto quente (a ponta), desenhada 4x maior e
    reduzida: o contorno sai suave em qualquer tamanho."""
    grande = tamanho * 4
    fator = grande / 24
    pontos = [(x * fator, y * fator) for x, y in _SETA_DO_TATO]
    raio = _RAIO_DO_TATO * fator
    corpo = Image.new("L", (grande, grande), 0)
    d = ImageDraw.Draw(corpo)
    d.polygon(pontos, fill=255)
    d.line(pontos + [pontos[0]], fill=255, width=round(2 * raio))
    for x, y in pontos:
        d.ellipse((x - raio, y - raio, x + raio, y + raio), fill=255)
    halo = corpo.filter(ImageFilter.GaussianBlur(fator * 0.9)).point(lambda a: a * 110 // 255)
    miolo = corpo.filter(ImageFilter.MinFilter(max(3, round(fator * 0.9)) | 1))
    img = Image.new("RGBA", (grande, grande), (0, 0, 0, 0))
    for cores, mascara in ((_CORES_DA_SETA, halo), (_CORES_DA_SETA, corpo),
                           (_clarear(_CORES_DA_SETA, 0.55), ImageChops.subtract(corpo, miolo))):
        tinta = _degrade(cores, grande, grande).convert("RGBA")
        tinta.putalpha(mascara)
        img.alpha_composite(tinta)
    pequena = img.resize((tamanho, tamanho), Image.LANCZOS)
    return pequena, (round(PONTA_DO_TATO[0] * tamanho / 24), round(PONTA_DO_TATO[1] * tamanho / 24))


def com_cursor(imagem: Image.Image, mouse: Optional[Tuple[int, int]], escala: float = 1.0,
               com_seta: bool = False, agente: str = "Tato") -> Image.Image:
    """Compõe na captura o cursor da pessoa ou do agente ativo."""
    if mouse is None:
        return imagem
    base = imagem.convert("RGBA")
    if com_seta:
        desenho, (qx, qy) = seta(round(32 * max(0.6, escala)))
        base.alpha_composite(desenho, (max(0, mouse[0] - qx), max(0, mouse[1] - qy)))
        return base.convert(imagem.mode)
    d = ImageDraw.Draw(base)
    fator = max(0.6, escala) * 1.1
    pontos = [(mouse[0] + (px - 4) * fator, mouse[1] + (py - 2) * fator) for px, py in _SETA]
    d.polygon(pontos, fill=(255, 255, 255, 255), outline=(17, 17, 17, 255))
    d.line(pontos + [pontos[0]], fill=(17, 17, 17, 255), width=max(1, round(1.4 * fator)))
    return base.convert(imagem.mode)


__all__ = ["mouse_na_pilula", "onde_esta_o_mouse", "PALETA", "Regiao", "com_cursor", "desenhar", "fonte", "mouse_perto",
           "seta"]
