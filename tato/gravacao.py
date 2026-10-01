"""Grava a tela em quadros e monta GIFs para o MCP e o README.

    python scripts/gravar_gif.py gravar gravacoes/precos
    python scripts/gravar_gif.py montar roteiro.json assets/gifs/pesquisa-de-preco.gif

`gravar` para com Ctrl+C ou quando aparece um arquivo PARAR na pasta. Quadro
igual ao anterior não é salvo: vira tempo a mais no anterior.

O roteiro é uma lista de trechos, na ordem. Num trecho, `borrar` esconde uma área
num intervalo, `recorte` corta o quadro e `parado` encurta as esperas:

    [{"titulo": "Porklike", "sub": "jogo por turnos"},
     {"pasta": "gravacoes/porklike", "de": 3, "ate": 48, "legenda": "..."}]

Grave com `TATO_GRAVAR=1` no Tato, ou a moldura não aparece no vídeo.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageGrab

from .computador.computador_desenho import PALETA, com_cursor, fonte

FUNDO = (11, 13, 18)
TEXTO = (236, 238, 243)
APAGADO = (150, 156, 170)


def _mouse():
    ponto = (ctypes.c_long * 2)()
    ctypes.windll.user32.GetCursorPos(ponto)
    return ponto[0], ponto[1]


def _agente_no_mouse() -> bool:
    # O Tato deixa este marcador enquanto a seta dele está no lugar do cursor.
    return (Path(tempfile.gettempdir()) / "tato-cursor-trocado").exists()


def gravar(pasta: Path, fps: float, largura: int, caixa, minutos: float, *,
           parada: threading.Event | None = None, anunciar: bool = True) -> dict:
    ctypes.windll.user32.SetProcessDPIAware()
    pasta.mkdir(parents=True, exist_ok=True)
    parar = pasta / "PARAR"
    parar.unlink(missing_ok=True)
    quadros, anterior, inicio = [], None, time.monotonic()
    intervalo = 1 / fps
    if anunciar:
        print(f"gravando em {pasta} (Ctrl+C ou crie {parar} para parar)", flush=True)
    try:
        while not parar.exists() and not (parada and parada.is_set()) and time.monotonic() - inicio < minutos * 60:
            agora = time.monotonic()
            tela = ImageGrab.grab()
            x, y = _mouse()
            tela = com_cursor(tela, (x, y), com_seta=_agente_no_mouse())
            if caixa:
                tela = tela.crop(caixa)
            if tela.width > largura:
                tela = tela.resize((largura, round(tela.height * largura / tela.width)), Image.LANCZOS)
            tela = tela.convert("RGB")
            momento = round((agora - inicio) * 1000)
            if anterior is None or ImageChops.difference(tela, anterior).getbbox() is not None:
                nome = f"{len(quadros):05d}.png"
                tela.save(pasta / nome, optimize=False, compress_level=1)
                quadros.append({"arquivo": nome, "ms": momento})
                anterior = tela
            time.sleep(max(0.0, intervalo - (time.monotonic() - agora)))
    except KeyboardInterrupt:
        pass
    fim = round((time.monotonic() - inicio) * 1000)
    (pasta / "quadros.json").write_text(json.dumps({"fim_ms": fim, "quadros": quadros}, indent=1), encoding="utf-8")
    parar.unlink(missing_ok=True)
    resultado = {"quadros": len(quadros), "duracao_s": round(fim / 1000, 2), "pasta": str(pasta)}
    if anunciar:
        print(f"{len(quadros)} quadros diferentes em {fim / 1000:.1f} s", flush=True)
    return resultado


def _juntar(quadros, minimo_ms: int):
    """Quadros mais curtos que `minimo_ms` somam o tempo no seguinte, que é mais novo."""
    juntos = []
    for img, ms in quadros:
        if juntos and juntos[-1][1] < minimo_ms:
            juntos[-1] = (img, juntos[-1][1] + ms)
        else:
            juntos.append((img, ms))
    return juntos


def _borrado(img: Image.Image, caixa) -> Image.Image:
    """A área vira borrão: dado da pessoa que apareceu sem querer (mapa, endereço)."""
    caixa = tuple(int(v) for v in caixa)
    img = img.copy()
    img.paste(img.crop(caixa).filter(ImageFilter.GaussianBlur(14)), caixa[:2])
    return img


def _mudanca(a: Image.Image, b: Image.Image, area=None) -> float:
    """Fração da tela (ou da `area`) que mudou de um quadro para o outro, numa versão pequena."""
    if area:
        a, b = a.crop(tuple(area)), b.crop(tuple(area))
    pequeno = (240, round(240 * a.height / a.width))
    diferenca = ImageChops.difference(a.convert("L").resize(pequeno), b.convert("L").resize(pequeno))
    return diferenca.point(lambda p: 255 if p > 24 else 0).histogram()[255] / (pequeno[0] * pequeno[1])


def _trecho(pasta: Path, de: float, ate, pausa_max: int, velocidade: float, borrar=(), parado=0.0, area=None, recorte=None):
    """`parado`: quadro que muda menos que essa fração da tela conta como espera
    (o agente pensando); a espera seguida soma no máximo `pausa_max`. `area` limita a
    medida a um pedaço, como a tela do jogo sem o título da janela."""
    dados = json.loads((pasta / "quadros.json").read_text(encoding="utf-8"))
    quadros, fim = dados["quadros"], dados["fim_ms"]
    de_ms, ate_ms = de * 1000, (ate * 1000 if ate is not None else fim)
    saida, anterior, esperando = [], None, 0.0
    for i, q in enumerate(quadros):
        proximo = quadros[i + 1]["ms"] if i + 1 < len(quadros) else fim
        inicio, termino = max(q["ms"], de_ms), min(proximo, ate_ms)
        if termino <= inicio:
            continue
        ms = min(pausa_max, (termino - inicio) / velocidade)
        img = Image.open(pasta / q["arquivo"]).convert("RGB")
        if parado and area is None:
            # Sem área, o título da janela e a barra de tarefas ficam de fora: o
            # relógio e o fps no título mudam sem ninguém mexer.
            area = (0, round(img.height * 0.05), img.width, round(img.height * 0.93))
        if parado and anterior is not None and _mudanca(img, anterior, area) < parado:
            ms = min(ms, pausa_max - esperando)
            esperando += ms
            if ms < 20:
                continue
        else:
            esperando = 0.0
        anterior = img
        for b in borrar:
            # Vale para o quadro que fica na tela em algum momento do intervalo.
            if q["ms"] < b["ate"] * 1000 and proximo > b["de"] * 1000:
                img = _borrado(img, b["caixa"])
        if recorte:
            img = img.crop(tuple(recorte))
        saida.append((img, max(20, round(ms))))
    return saida


def _faixa(largura: int, altura: int) -> Image.Image:
    faixa = Image.new("RGB", (len(PALETA), 1))
    faixa.putdata(list(PALETA))
    return faixa.resize((largura, 1), Image.BILINEAR).resize((largura, altura), Image.NEAREST)


def _cartela(tamanho, titulo: str, sub: str = "") -> Image.Image:
    w, h = tamanho
    img = Image.new("RGB", tamanho, FUNDO)
    d = ImageDraw.Draw(img)
    grande, pequena = fonte("forte", max(18, h // 11)), fonte("normal", max(12, h // 24))
    caixa = d.textbbox((0, 0), titulo, font=grande)
    tw, th = caixa[2] - caixa[0], caixa[3] - caixa[1]
    y = h // 2 - th - (h // 40 if sub else 0)
    d.text(((w - tw) // 2, y), titulo, font=grande, fill=TEXTO)
    img.paste(_faixa(min(tw, w // 3), 4), ((w - min(tw, w // 3)) // 2, y + th + h // 30))
    if sub:
        caixa = d.textbbox((0, 0), sub, font=pequena)
        d.text(((w - (caixa[2] - caixa[0])) // 2, y + th + h // 30 + 18), sub, font=pequena, fill=APAGADO)
    return img


def _legendar(img: Image.Image, texto: str) -> Image.Image:
    img = img.copy()
    d = ImageDraw.Draw(img, "RGBA")
    letra = fonte("forte", max(12, img.height // 30))
    largura_max = max(40, img.width - 36)
    linhas: list[str] = []
    for palavra in texto.split():
        if linhas and d.textlength(linhas[-1] + " " + palavra, font=letra) <= largura_max:
            linhas[-1] += " " + palavra
        else:
            linhas.append(palavra)
        while d.textlength(linhas[-1], font=letra) > largura_max:
            parte = linhas.pop()
            corte = max((i for i in range(1, len(parte) + 1)
                         if d.textlength(parte[:i], font=letra) <= largura_max), default=1)
            linhas.extend([parte[:corte], parte[corte:]])
    altura_linha = max(15, round(img.height / 25))
    bloco_h = len(linhas) * altura_linha
    x, y = 18, img.height - bloco_h - max(12, img.height // 20)
    d.rounded_rectangle((x - 8, y - 8, img.width - x + 8, y + bloco_h + 10),
                        radius=10, fill=FUNDO + (225,))
    for n, linha in enumerate(linhas):
        caixa = d.textbbox((0, 0), linha, font=letra)
        d.text(((img.width - d.textlength(linha, font=letra)) / 2,
                y + n * altura_linha - caixa[1]), linha, font=letra, fill=TEXTO)
    return img


def montar(roteiro: Path, saida: Path, pausa_max: int, velocidade: float, cartela_ms: int,
           fps: float, *, anunciar: bool = True) -> dict:
    trechos = json.loads(roteiro.read_text(encoding="utf-8"))
    base = roteiro.parent
    quadros, tamanho = [], None
    for t in trechos:
        if "pasta" in t:
            pedaco = _trecho(base / t["pasta"], t.get("de", 0), t.get("ate"), t.get("pausa_max", pausa_max),
                             t.get("velocidade", velocidade), t.get("borrar", ()), t.get("parado", 0.0),
                             t.get("area"), t.get("recorte"))
            pedaco = _juntar(pedaco, round(1000 / fps))
            if t.get("legenda"):
                pedaco = [(_legendar(img, t["legenda"]), ms) for img, ms in pedaco]
            tamanho = tamanho or (pedaco[0][0].size if pedaco else None)
            quadros.extend(pedaco)
        else:
            quadros.append(({"titulo": t["titulo"], "sub": t.get("sub", "")}, t.get("ms", cartela_ms)))
    if tamanho is None:
        raise ValueError("o roteiro não tem nenhum trecho gravado")
    imagens, tempos = [], []
    for img, ms in quadros:
        if isinstance(img, dict):
            img = _cartela(tamanho, img["titulo"], img["sub"])
        elif img.size != tamanho:
            img = img.resize(tamanho, Image.LANCZOS)
        imagens.append(img.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE))
        tempos.append(ms)
    saida.parent.mkdir(parents=True, exist_ok=True)
    imagens[0].save(saida, save_all=True, append_images=imagens[1:], duration=tempos, loop=0, optimize=True,
                    disposal=1)
    resultado = {"arquivo": str(saida), "quadros": len(imagens),
                 "duracao_s": round(sum(tempos) / 1000, 2), "bytes": saida.stat().st_size}
    if anunciar:
        print(f"{saida}: {len(imagens)} quadros, {sum(tempos) / 1000:.1f} s, "
              f"{saida.stat().st_size / 1e6:.1f} MB", flush=True)
    return resultado


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="comando", required=True)
    g = sub.add_parser("gravar")
    g.add_argument("pasta", type=Path)
    g.add_argument("--fps", type=float, default=10)
    g.add_argument("--largura", type=int, default=960)
    g.add_argument("--caixa", help="x0,y0,x1,y1 da tela, em pixels")
    g.add_argument("--minutos", type=float, default=15)
    m = sub.add_parser("montar")
    m.add_argument("roteiro", type=Path)
    m.add_argument("saida", type=Path)
    m.add_argument("--pausa-max", type=int, default=1500, help="teto de um quadro parado, em ms")
    m.add_argument("--velocidade", type=float, default=1.0)
    m.add_argument("--cartela-ms", type=int, default=1600)
    m.add_argument("--fps", type=float, default=6, help="teto de quadros por segundo no GIF")
    a = p.parse_args()
    if a.comando == "gravar":
        caixa = tuple(int(v) for v in a.caixa.split(",")) if a.caixa else None
        gravar(a.pasta, a.fps, a.largura, caixa, a.minutos)
    else:
        montar(a.roteiro, a.saida, a.pausa_max, a.velocidade, a.cartela_ms, a.fps)


if __name__ == "__main__":
    main()
