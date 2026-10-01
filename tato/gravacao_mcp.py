"""Gravações locais da tela, controladas por uma ferramenta MCP.

Uma gravação pertence à conexão que a iniciou. Os identificadores são UUIDs;
nenhum argumento da ferramenta vira caminho de arquivo arbitrário.
"""
from __future__ import annotations

import asyncio
import base64
import ctypes
import io
import json
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .config import pasta_do_tato
from .gravacao import _mudanca, gravar, montar


@dataclass
class _Captura:
    ident: str
    sessao: str
    pasta: Path
    parada: threading.Event
    thread: threading.Thread
    indicador: threading.Thread
    criada_em: float
    resultado: dict | None = None
    erro: str | None = None


_trava = threading.RLock()
_ativa: _Captura | None = None
_NOME_QUADRO = re.compile(r"[0-9]{5}\.png\Z")


def _abrir_indicador(parada: threading.Event, pronto: threading.Event,
                     status: dict, minutos: float) -> None:
    """Aviso e botão locais. Se a janela sumir, a gravação termina."""
    janela = None
    try:
        import tkinter as tk

        janela = tk.Tk()
        janela.title("Tato — gravando")
        janela.configure(bg="#17191f")
        janela.resizable(False, False)
        janela.attributes("-topmost", True)
        largura, altura = 295, 77
        x = max(0, janela.winfo_screenwidth() - largura - 24)
        janela.geometry(f"{largura}x{altura}+{x}+24")
        tk.Label(janela, text="● Tato está gravando a tela", bg="#17191f", fg="#ff7373",
                 font=("Segoe UI", 11, "bold")).pack(pady=(7, 0))
        linha = tk.Frame(janela, bg="#17191f")
        linha.pack(fill="x", padx=12)
        tk.Label(linha, text=f"Local · até {minutos:g} min", bg="#17191f", fg="white").pack(side="left")
        tk.Button(linha, text="Parar", command=parada.set).pack(side="right")
        janela.protocol("WM_DELETE_WINDOW", parada.set)
        janela.update()
        pronto.set()

        def vigiar() -> None:
            if parada.is_set():
                janela.destroy()
            else:
                janela.after(150, vigiar)

        janela.after(150, vigiar)
        janela.mainloop()
    except Exception as exc:
        status["erro"] = f"não consegui mostrar o aviso de gravação: {type(exc).__name__}: {exc}"
        parada.set()
    finally:
        pronto.set()
        parada.set()


def _raiz() -> Path:
    return pasta_do_tato() / "gravacoes"


def _pasta(ident: Any) -> Path:
    try:
        nome = str(uuid.UUID(str(ident)))
    except (ValueError, AttributeError) as exc:
        raise ValueError("id de gravação inválido; use o id devolvido por iniciar") from exc
    pasta = _raiz() / nome
    if not (pasta / "quadros.json").is_file():
        raise ValueError("gravação não encontrada ou ainda não finalizada")
    return pasta


def _dados(pasta: Path) -> dict:
    dados = json.loads((pasta / "quadros.json").read_text(encoding="utf-8"))
    quadros = dados.get("quadros")
    if (not isinstance(quadros, list) or len(quadros) > 12000
            or not isinstance(dados.get("fim_ms"), int) or dados["fim_ms"] < 0):
        raise ValueError("índice de quadros inválido")
    anterior = -1
    for quadro in quadros:
        if not isinstance(quadro, dict):
            raise ValueError("índice de quadros inválido")
        nome, ms = quadro.get("arquivo"), quadro.get("ms")
        if not isinstance(nome, str) or not _NOME_QUADRO.fullmatch(nome) or not isinstance(ms, int) or ms < anterior:
            raise ValueError("índice de quadros inválido")
        anterior = ms
    if anterior > dados["fim_ms"]:
        raise ValueError("índice de quadros inválido")
    return dados


def _numero(valor: Any, nome: str, minimo: float, maximo: float) -> float:
    try:
        numero = float(valor)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{nome} deve ser um número entre {minimo} e {maximo}") from exc
    if not minimo <= numero <= maximo:
        raise ValueError(f"{nome} deve ficar entre {minimo} e {maximo}")
    return numero


def _retangulo(valor: Any, nome: str) -> list[int]:
    if not isinstance(valor, (list, tuple)) or len(valor) != 4:
        raise ValueError(f"{nome} deve ser [x0, y0, x1, y1]")
    numeros = [int(_numero(v, nome, 0, 10000)) for v in valor]
    if numeros[2] <= numeros[0] or numeros[3] <= numeros[1]:
        raise ValueError(f"{nome} precisa ter largura e altura positivas")
    if (numeros[2] - numeros[0]) * (numeros[3] - numeros[1]) > 3840 * 2160:
        raise ValueError(f"{nome} cobre uma área grande demais")
    return numeros


def iniciar(sessao: str, argumentos: dict) -> dict:
    global _ativa
    if sys.platform != "win32":
        return {"ok": False, "erro": "a gravação de tela do Tato está disponível apenas no Windows por enquanto"}
    fps = _numero(argumentos.get("fps", 10), "fps", 1, 12)
    largura = int(_numero(argumentos.get("largura", 960), "largura", 320, 1920))
    minutos = _numero(argumentos.get("minutos", 15), "minutos", 0.1, 15)
    caixa = _retangulo(argumentos["caixa"], "caixa") if argumentos.get("caixa") is not None else None
    with _trava:
        if _ativa and _ativa.thread.is_alive():
            return {"ok": False, "erro": "já existe uma gravação ativa; pare-a antes de iniciar outra"}
        ident = str(uuid.uuid4())
        pasta = _raiz() / ident
        ctypes.windll.user32.SetProcessDPIAware()
        parada = threading.Event()
        pronto = threading.Event()
        status: dict = {}
        indicador = threading.Thread(target=_abrir_indicador, args=(parada, pronto, status, minutos),
                                     name="tato-aviso-gravacao", daemon=True)
        indicador.start()
        if not pronto.wait(timeout=4) or status.get("erro") or parada.is_set():
            parada.set()
            return {"ok": False, "erro": status.get("erro") or
                    "não consegui confirmar o aviso visível; a tela não foi gravada"}
        captura = _Captura(ident, sessao, pasta, parada, threading.Thread(), indicador, time.time())

        def trabalhar() -> None:
            try:
                captura.resultado = gravar(pasta, fps, largura, tuple(caixa) if caixa else None,
                                           minutos, parada=parada, anunciar=False)
            except Exception as exc:
                captura.erro = f"a captura falhou: {type(exc).__name__}: {exc}"
            finally:
                parada.set()

        captura.thread = threading.Thread(target=trabalhar, name="tato-gravacao", daemon=True)
        _ativa = captura
        captura.thread.start()
    return {"ok": True, "acao": "iniciar", "id": ident, "gravando": True,
            "pasta": str(pasta), "limite_minutos": minutos,
            "aviso": "A tela inteira pode incluir dados pessoais. A gravação fica apenas neste computador."}


def estado(sessao: str) -> dict:
    with _trava:
        captura = _ativa
        if captura is None or captura.sessao != sessao:
            return {"ok": True, "gravando": False}
        duracao = (captura.resultado or {}).get("duracao_s")
        if duracao is None:
            duracao = round(time.time() - captura.criada_em, 1)
        return {"ok": captura.erro is None, "id": captura.ident, "gravando": captura.thread.is_alive(),
                "duracao_s": duracao,
                "resultado": captura.resultado, "erro": captura.erro}


def parar(sessao: str) -> dict:
    with _trava:
        captura = _ativa
        if captura is None or captura.sessao != sessao:
            return {"ok": False, "erro": "esta conexão não tem uma gravação ativa"}
        captura.parada.set()
    captura.thread.join(timeout=10)
    if captura.thread.is_alive():
        return {"ok": False, "erro": "a gravação ainda está terminando; consulte estado antes de montar"}
    captura.indicador.join(timeout=2)
    if captura.erro:
        return {"ok": False, "erro": captura.erro, "id": captura.ident}
    return {"ok": True, "acao": "parar", "id": captura.ident, **(captura.resultado or {})}


def _trechos(argumentos: dict, duracao_s: float, duracoes: dict[str, float] | None = None) -> list[dict]:
    entrada = argumentos.get("trechos") or [{"de": 0, "ate": duracao_s}]
    if not isinstance(entrada, list) or not 1 <= len(entrada) <= 20:
        raise ValueError("trechos deve conter entre 1 e 20 itens")
    saida, tempo_total = [], 0.0
    for item in entrada:
        if not isinstance(item, dict):
            raise ValueError("cada trecho deve ser um objeto")
        if "titulo" in item:
            titulo = str(item["titulo"]).strip()
            if not 1 <= len(titulo) <= 100:
                raise ValueError("título da cartela deve ter entre 1 e 100 caracteres")
            saida.append({"titulo": titulo, "sub": str(item.get("sub") or "")[:140],
                          "ms": int(_numero(item.get("ms", 1600), "ms", 200, 5000))})
            continue
        outro_id = item.get("id")
        if outro_id is not None:
            try:
                outro_id = str(uuid.UUID(str(outro_id)))
            except (ValueError, AttributeError) as exc:
                raise ValueError("id do trecho inválido; use um id devolvido por iniciar") from exc
            if duracoes is None or outro_id not in duracoes:
                raise ValueError("id do trecho não pertence a uma gravação local finalizada")
        limite = duracoes[outro_id] if outro_id else duracao_s
        inicio = _numero(item.get("de", 0), "de", 0, limite)
        fim = _numero(item.get("ate", limite), "ate", 0, limite)
        if fim <= inicio:
            raise ValueError("um trecho precisa terminar depois de começar")
        velocidade = _numero(item.get("velocidade", 1), "velocidade", 0.25, 20)
        tempo_total += (fim - inicio) / velocidade
        if tempo_total > 90:
            raise ValueError("o GIF teria mais de 90 s; encurte os trechos ou aumente a velocidade")
        trecho = {"pasta": f"../{outro_id}" if outro_id else ".", "de": inicio, "ate": fim, "velocidade": velocidade,
                  "parado": _numero(item.get("parado", 0), "parado", 0, 1)}
        if "legenda" in item:
            legenda = str(item["legenda"]).strip()
            if not 1 <= len(legenda) <= 120:
                raise ValueError("legenda deve ter entre 1 e 120 caracteres")
            trecho["legenda"] = legenda
        for campo in ("area", "recorte"):
            if campo in item:
                trecho[campo] = _retangulo(item[campo], campo)
        if "borrar" in item:
            borroes = item["borrar"]
            if not isinstance(borroes, list) or len(borroes) > 10:
                raise ValueError("borrar aceita até 10 intervalos")
            if any(not isinstance(b, dict) or not {"de", "ate", "caixa"} <= b.keys() for b in borroes):
                raise ValueError("cada borrão precisa de de, ate e caixa")
            trecho["borrar"] = [{"de": _numero(b["de"], "borrar.de", 0, duracao_s),
                                  "ate": _numero(b["ate"], "borrar.ate", 0, duracao_s),
                                  "caixa": _retangulo(b["caixa"], "borrar.caixa")}
                                 for b in borroes]
            if any(b["ate"] <= b["de"] for b in trecho["borrar"]):
                raise ValueError("intervalo de borrão precisa terminar depois de começar")
        saida.append(trecho)
    if not any("pasta" in t for t in saida):
        raise ValueError("inclua ao menos um trecho da gravação, não só cartelas")
    return saida


def _nome_do_gif(nome) -> str:
    """Nome do arquivo de saída: cada montagem pode ter o seu, sem sobrescrever a anterior."""
    limpo = re.sub(r"[^\w-]+", "-", str(nome or "").strip()).strip("-")[:60]
    return limpo or "resumo"


def montar_local(argumentos: dict) -> dict:
    pasta = _pasta(argumentos.get("id"))
    dados = _dados(pasta)
    duracao_s = dados["fim_ms"] / 1000
    ids_extras = {str(item["id"]) for item in (argumentos.get("trechos") or [])
                  if isinstance(item, dict) and item.get("id") is not None}
    if len(ids_extras) > 5:
        raise ValueError("a montagem aceita até 5 gravações adicionais")
    duracoes = {}
    for ident in ids_extras:
        pasta_extra = _pasta(ident)
        dados_extras = _dados(pasta_extra)
        if not dados_extras["quadros"]:
            raise ValueError(f"a gravação {ident} não contém quadros")
        duracoes[str(uuid.UUID(str(ident)))] = dados_extras["fim_ms"] / 1000
    roteiro = _trechos(argumentos, duracao_s, duracoes)
    if not dados["quadros"]:
        raise ValueError("esta gravação não contém quadros para montar")
    for trecho in roteiro:
        if "pasta" not in trecho:
            continue
        pasta_trecho = (pasta / trecho["pasta"]).resolve()
        dados_trecho = _dados(pasta_trecho)
        with Image.open(pasta_trecho / dados_trecho["quadros"][0]["arquivo"]) as primeiro:
            largura, altura = primeiro.size
        for nome in ("area", "recorte"):
            caixa = trecho.get(nome)
            if caixa and (caixa[2] > largura or caixa[3] > altura):
                raise ValueError(f"{nome} ultrapassa o tamanho do quadro: {largura}x{altura}")
        for borrao in trecho.get("borrar", []):
            caixa = borrao["caixa"]
            if caixa[2] > largura or caixa[3] > altura:
                raise ValueError(f"borrar.caixa ultrapassa o tamanho do quadro: {largura}x{altura}")
    sufixo = uuid.uuid4().hex
    roteiro_temp = pasta / f"roteiro-{sufixo}.json"
    gif_temp = pasta / f"resumo-{sufixo}.gif"
    try:
        roteiro_temp.write_text(json.dumps(roteiro, ensure_ascii=False, indent=2), encoding="utf-8")
        resultado = montar(roteiro_temp, gif_temp, 1500, 1, 1600, 6, anunciar=False)
        gif_final = pasta / f"{_nome_do_gif(argumentos.get('nome'))}.gif"
        gif_temp.replace(gif_final)
        roteiro_temp.replace(pasta / "roteiro.json")
        return {"ok": True, "acao": "montar", "id": str(uuid.UUID(str(argumentos["id"]))),
                **resultado, "arquivo": str(gif_final)}
    finally:
        roteiro_temp.unlink(missing_ok=True)
        gif_temp.unlink(missing_ok=True)


def ver_local(argumentos: dict) -> tuple[dict, str]:
    """Folha pequena por tempo ou transição visual, sem expor quadros individuais em massa."""
    pasta = _pasta(argumentos.get("id"))
    dados = _dados(pasta)
    quadros = dados["quadros"]
    if not quadros:
        raise ValueError("esta gravação não contém quadros para visualizar")
    amostras = int(_numero(argumentos.get("amostras", 8), "amostras", 1, 12))
    duracao = dados["fim_ms"] / 1000
    inicio = _numero(argumentos.get("de", 0), "de", 0, duracao)
    fim = _numero(argumentos.get("ate", duracao), "ate", 0, duracao)
    if fim <= inicio:
        raise ValueError("em ver, ate deve ser maior que de")
    modo = argumentos.get("modo", "intervalo")
    if modo not in ("intervalo", "mudancas"):
        raise ValueError("modo deve ser intervalo ou mudancas")
    candidatos = [i for i, q in enumerate(quadros) if inicio * 1000 <= q["ms"] <= fim * 1000]
    if not candidatos:
        raise ValueError("não há quadros neste intervalo; escolha outro de/ate")
    area = _retangulo(argumentos["area"], "area") if argumentos.get("area") is not None else None
    if area:
        with Image.open(pasta / quadros[candidatos[0]]["arquivo"]) as primeiro:
            if area[2] > primeiro.width or area[3] > primeiro.height:
                raise ValueError(f"area ultrapassa o tamanho do quadro: {primeiro.width}x{primeiro.height}")
    mudancas_detectadas = None
    if modo == "mudancas":
        if fim - inicio > 30:
            raise ValueError("para ver mudanças, escolha um intervalo de até 30 s com de/ate")
        pontuacoes = []
        anterior = None
        for indice in candidatos:
            with Image.open(pasta / quadros[indice]["arquivo"]) as original:
                atual = original.convert("RGB")
            if anterior is not None:
                pontuacoes.append((_mudanca(anterior, atual, area), indice))
            anterior = atual
        mudancas_detectadas = sum(pontos > 0 for pontos, _ in pontuacoes)
        escolhidos = set()
        for pontos, indice in sorted(pontuacoes, reverse=True):
            if pontos <= 0:
                break
            antes = quadros[indice - 1]
            if indice - 1 not in candidatos or antes["ms"] < inicio * 1000:
                continue
            novos = {indice - 1, indice} - escolhidos
            if len(escolhidos) + len(novos) > amostras:
                continue
            escolhidos.update(novos)
            if len(escolhidos) >= amostras:
                break
        if not escolhidos:
            escolhidos.add(candidatos[0])
            if amostras > 1:
                escolhidos.add(candidatos[-1])
        indices = sorted(escolhidos)
    else:
        indices = sorted({candidatos[round(i * (len(candidatos) - 1) / max(1, amostras - 1))]
                          for i in range(amostras)})
    colunas, largura, altura = 2, 300, 210
    folha = Image.new("RGB", (colunas * largura, ((len(indices) + 1) // 2) * altura), "#17191f")
    desenhar = ImageDraw.Draw(folha)
    for posicao, indice in enumerate(indices):
        quadro = quadros[indice]
        with Image.open(pasta / quadro["arquivo"]) as original:
            img = original.convert("RGB")
        if area:
            img = img.crop(tuple(area))
        img.thumbnail((largura - 16, altura - 38), Image.Resampling.LANCZOS)
        x, y = (posicao % 2) * largura, (posicao // 2) * altura
        folha.paste(img, (x + (largura - img.width) // 2, y + 8))
        desenhar.text((x + 8, y + altura - 25), f"#{indice} · {quadro['ms'] / 1000:.2f} s", fill="white")
    saida = io.BytesIO()
    folha.save(saida, format="PNG")
    imagem = "data:image/png;base64," + base64.b64encode(saida.getvalue()).decode("ascii")
    resumo = {"ok": True, "acao": "ver", "id": str(uuid.UUID(str(argumentos["id"]))),
              "modo": modo, "amostras": len(indices), "duracao_s": duracao,
              "intervalo": {"de": inicio, "ate": fim},
              "quadros": [{"indice": i, "segundo": quadros[i]["ms"] / 1000} for i in indices]}
    if modo == "mudancas":
        resumo["mudancas_detectadas"] = mudancas_detectadas
        if mudancas_detectadas == 0:
            resumo["aviso"] = ("nenhuma mudança visual detectada neste intervalo; algo mais rápido "
                               "que a captura pode não aparecer")
    return resumo, imagem


async def executar(argumentos: dict, state: Any) -> dict:
    acao = argumentos.get("acao")
    sessao = state.session_id
    try:
        if acao == "iniciar":
            return iniciar(sessao, argumentos)
        if acao == "parar":
            return await asyncio.to_thread(parar, sessao)
        if acao == "estado":
            return estado(sessao)
        if acao == "montar":
            return await asyncio.to_thread(montar_local, argumentos)
        if acao == "ver":
            resultado, imagem = await asyncio.to_thread(ver_local, argumentos)
            state.print_da_tela = {"data_url": imagem}
            return resultado
        return {"ok": False, "erro": "ação de gravação desconhecida; use iniciar, parar, estado, ver ou montar"}
    except (ValueError, KeyError, OSError) as exc:
        return {"ok": False, "erro": str(exc)}


def encerrar(sessao: str) -> None:
    """A conexão morreu: não deixar uma gravação de tela rodando sem agente."""
    with _trava:
        captura = _ativa
    if captura and captura.sessao == sessao and captura.thread.is_alive():
        parar(sessao)
