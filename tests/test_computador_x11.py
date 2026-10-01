"""O computer use numa tela X de verdade (Xvfb): o gesto chega ao programa.

Sobe um Xvfb próprio em 1920x1080, maior que o print (1456 de lado), para a
conversão de coordenada aparecer. Um "programa" de teste registra o clique e o
texto que recebe. Pula sem Xvfb ou sem python-xlib.
"""
import asyncio
import os
import shutil
import subprocess
import threading
import time

import pytest

xlib = pytest.importorskip("Xlib")
if not shutil.which("Xvfb"):
    pytest.skip("sem Xvfb", allow_module_level=True)

from Xlib import X, XK, display  # noqa: E402

from tato.estado import EstadoDoTurno
from tato.computador import computador
from tato.computador.computador_desenho import PALETA  # noqa: E402
from tato.computador.computador_x11 import BackendX11  # noqa: E402

# O degradê da borda corre da esquerda para a direita: a beira esquerda é a primeira cor.
COR_DE_DESTAQUE = PALETA[0]


@pytest.fixture(scope="module")
def tela():
    """Um Xvfb só nosso. Com `pytest -n 4`, dois workers sobem o Xvfb ao mesmo
    tempo: número fixo colidia, e o `-displayfd` também dava o mesmo número aos
    dois (um morria, o outro teste conectava numa tela que caiu). Número
    sorteado, e confere que quem respondeu é o NOSSO processo."""
    import random

    for numero in random.sample(range(200, 900), 20):
        if os.path.exists(f"/tmp/.X{numero}-lock"):
            continue
        # `-noreset`: sem ele o Xvfb reinicia quando o último cliente sai, e o
        # teste seguinte conectava bem no meio do reinício (conexão caída).
        processo = subprocess.Popen(["Xvfb", f":{numero}", "-screen", "0", "1920x1080x24", "-nolisten", "tcp",
                                     "-noreset"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if processo.poll() is not None:
                break
            try:
                display.Display(f":{numero}").close()
                break
            except Exception:
                time.sleep(0.1)
        time.sleep(0.1)
        if processo.poll() is None:
            yield f":{numero}"
            processo.kill()
            processo.wait()
            return
    pytest.skip("Xvfb não subiu")


class Programa:
    """Uma janela em (400, 300), 400x300, que anota clique e texto."""

    def __init__(self, nome):
        self.d = display.Display(nome)
        raiz = self.d.screen().root
        self.janela = raiz.create_window(
            400, 300, 400, 300, 0, self.d.screen().root_depth, X.InputOutput, X.CopyFromParent,
            background_pixel=self.d.screen().white_pixel,
            event_mask=X.ButtonPressMask | X.KeyPressMask)
        self.janela.set_wm_name("Programa de teste")
        self.janela.map()
        self.d.sync()
        time.sleep(0.2)
        self.janela.set_input_focus(X.RevertToParent, X.CurrentTime)
        self.d.sync()
        self.cliques, self.texto = [], ""
        self._vivo = True
        threading.Thread(target=self._ouvir, daemon=True).start()

    def _ouvir(self):
        while self._vivo:
            if not self.d.pending_events():
                time.sleep(0.01)
                continue
            ev = self.d.next_event()
            if ev.type == X.MappingNotify:
                self.d.refresh_keyboard_mapping(ev)
            elif ev.type == X.ButtonPress:
                self.cliques.append((ev.event_x, ev.event_y, ev.detail))
            elif ev.type == X.KeyPress:
                simbolo = self.d.keycode_to_keysym(ev.detail, 1 if ev.state & X.ShiftMask else 0) \
                    or self.d.keycode_to_keysym(ev.detail, 0)
                if 0x20 <= simbolo <= 0xFF:
                    self.texto += chr(simbolo)
                elif simbolo > 0x01000000:
                    self.texto += chr(simbolo - 0x01000000)
                elif simbolo == XK.XK_Return:
                    self.texto += "\n"

    def fechar(self):
        self._vivo = False
        self.janela.destroy()
        self.d.close()


@pytest.fixture
def mundo(tela, monkeypatch):
    backend = BackendX11(tela)
    computador.usar_backend(backend)
    programa = Programa(tela)
    yield backend, programa
    programa.fechar()
    computador._limpar_para_testes()
    computador.usar_backend(None)
    backend.fechar()


def _state(sessao="x11"):
    from tato.permissoes import limpar_sessao, responder

    limpar_sessao(sessao)
    responder("computador clicar", "uma_vez", sessao)
    state = EstadoDoTurno.new(sessao, "u")
    state.funcoes_ativas = ["computador"]
    return state


def _print_do_modelo(state):
    import base64
    import io

    from PIL import Image

    dados = state.print_da_tela["data_url"].split(",", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(dados))).convert("RGB")


def _pixel(nome, x, y):
    from PIL import ImageGrab

    return ImageGrab.grab(xdisplay=nome).getpixel((x, y))


async def _pixel_quando(nome, x, y, cor, prazo=2.0):
    """O pixel, esperando ele ficar `cor`. A moldura sai da tela durante o
    print automático depois do gesto e volta pela thread dela: uma amostra
    logo depois do gesto pode pegar esse intervalo."""
    fim = asyncio.get_running_loop().time() + prazo
    while True:
        atual = await asyncio.to_thread(_pixel, nome, x, y)
        if atual == cor or asyncio.get_running_loop().time() > fim:
            return atual
        await asyncio.sleep(0.05)


async def test_o_clique_e_o_texto_chegam_ao_programa(tela, mundo):
    backend, programa = mundo
    state = _state()
    fator = computador.escala((1920, 1080))

    async def turno():
        ver = await computador.executar({"acao": "ver"}, state)
        # O centro do programa na tela, (600, 450), no espaço do print.
        clique = await computador.executar({"acao": "clicar", "x": 600 * fator, "y": 450 * fator}, state)
        digitou = await computador.executar({"acao": "digitar", "texto": "Olá, ação\n"}, state)
        borda = await _pixel_quando(tela, 1, 540, COR_DE_DESTAQUE)
        durante = await asyncio.to_thread(backend.estado_da_moldura)
        return ver, clique, digitou, borda, _print_do_modelo(state), durante

    ver, clique, digitou, borda, do_modelo, durante = await asyncio.create_task(turno())
    # O fim do turno fecha a moldura num callback da task, que roda depois deste await.
    await asyncio.sleep(0)
    # Com o agente no mouse, o cursor e a seta dele; depois, volta o da pessoa.
    assert durante["seta"] and durante["cursor_escondido"]
    depois = await asyncio.to_thread(backend.estado_da_moldura)
    assert not depois["seta"] and not depois["cursor_escondido"]
    # A pessoa vê a moldura; o modelo, não: ela sai da tela durante o print.
    # O meio do painel das falas é cor lisa, que o encolhimento não mistura;
    # a borda mistura com o vizinho no encolhimento, então vale "puxado para o ciano".
    meio_do_painel = (round((1920 - 16 - 260) * fator), round((1080 - 16 - 45) * fator))
    assert do_modelo.getpixel(meio_do_painel) != (0x3F, 0x3F, 0x46)
    r, g, b = do_modelo.getpixel((0, 400))
    assert not (r < 120 and g > 170 and b > 190), "a borda saiu no print do modelo"
    await asyncio.sleep(0.3)
    assert ver["print"] == {"largura": 1456, "altura": 819}
    assert clique["ok"] and digitou["ok"], (clique, digitou)
    # Arredondar para o print e voltar erra no máximo um pixel e pouco.
    x, y, botao = programa.cliques[0]
    assert abs(x - 200) <= 2 and abs(y - 150) <= 2 and botao == 1
    assert programa.texto == "Olá, ação\n"
    assert borda == COR_DE_DESTAQUE  # a moldura estava na tela durante o turno
    await asyncio.sleep(0.2)
    assert _pixel(tela, 1, 540) != COR_DE_DESTAQUE  # e saiu com ele
    assert state.print_da_tela is None


async def test_janelas_acha_o_programa_em_foco(tela, mundo):
    r = await asyncio.create_task(computador.executar({"acao": "janelas"}, _state()))
    programa = next(j for j in r["janelas"] if j["titulo"] == "Programa de teste")
    assert programa["em_foco"]


async def test_o_atalho_de_verdade_para_o_turno(tela, mundo):
    """Ctrl+Alt+Shift+S apertado como a pessoa aperta (XTEST, outra conexão):
    o turno é cancelado, a moldura sai, e nenhum gesto roda depois."""
    _, programa = mundo
    state = _state()
    fator = computador.escala((1920, 1080))
    cliques_no_atalho = []

    async def turno():
        for i in range(200):
            await computador.executar({"acao": "clicar", "x": 600 * fator, "y": 450 * fator,
                                       "ver_depois": False}, state)
            if i == 2:
                pessoa = BackendX11(tela)
                for t in ("ctrl", "alt", "shift"):
                    pessoa.tecla_bruta(t, True)
                pessoa.tecla_bruta("s", True)
                pessoa.tecla_bruta("s", False)
                for t in ("shift", "alt", "ctrl"):
                    pessoa.tecla_bruta(t, False)
                pessoa.fechar()
            await asyncio.sleep(0.05)
            cliques_no_atalho.append(len(programa.cliques))

    tarefa = asyncio.create_task(turno())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(tarefa, 10)
    await asyncio.sleep(0.3)
    feitos = len(programa.cliques)
    await asyncio.sleep(0.3)
    assert len(programa.cliques) == feitos <= 5
    assert _pixel(tela, 1, 540) != COR_DE_DESTAQUE
