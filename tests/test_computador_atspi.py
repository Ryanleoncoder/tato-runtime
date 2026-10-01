"""Testa a leitura AT-SPI de controles em janelas GTK no Xvfb.

Confere papéis, foco, posição e ordem das camadas visíveis.
Ignora os testes quando falta uma dependência do ambiente gráfico.
"""
import os
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Atspi", "2.0")
    gi.require_version("Gtk", "3.0")
except ValueError:
    pytest.skip("sem Atspi ou Gtk no gi", allow_module_level=True)
pytest.importorskip("Xlib")
_LANCADOR = "/usr/libexec/at-spi-bus-launcher"
if not (shutil.which("Xvfb") and shutil.which("dbus-launch") and os.path.exists(_LANCADOR)):
    pytest.skip("sem Xvfb, dbus-launch ou at-spi-bus-launcher", allow_module_level=True)

from tato.computador import computador_atspi
from tato.computador.computador_elementos import filtrar

_JANELA = textwrap.dedent('''
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    w = Gtk.Window(title="Entrar no Portal")
    w.set_default_size(600, 360)
    caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=24)
    usuario = Gtk.Entry(); usuario.get_accessible().set_name("Usuário")
    senha = Gtk.Entry(visibility=False); senha.get_accessible().set_name("Senha")
    lembrar = Gtk.CheckButton(label="Lembrar de mim")
    entrar = Gtk.Button(label="Entrar")
    entrar.connect("clicked", lambda *_: print("CLICOU ENTRAR", flush=True))
    for item in (usuario, senha, lembrar, entrar):
        caixa.pack_start(item, False, False, 0)
    w.add(caixa)
    w.connect("destroy", Gtk.main_quit)
    w.show_all()
    senha.grab_focus()
    w.present()
    Gtk.main()
''')


_TRES_JANELAS = textwrap.dedent('''
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    def janela(titulo, x, y, largura, altura, botoes):
        w = Gtk.Window(title=titulo)
        w.set_default_size(largura, altura)
        w.move(x, y)
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=20)
        for nome in botoes:
            caixa.pack_start(Gtk.Button(label=nome), False, False, 0)
        w.add(caixa)
        return w

    b = janela("Janela B", 700, 50, 400, 300, ["Botão de B"])
    c = janela("Janela C", 300, 100, 400, 200, ["Botão de C"])
    a = janela("Janela A", 50, 50, 500, 300, ["Salvar", "Abrir"])
    for w in (b, c, a):
        w.show_all()
    menu = Gtk.Menu()
    for nome in ("Copiar", "Colar", "Excluir"):
        menu.append(Gtk.MenuItem(label=nome))
    menu.show_all()

    def abrir():
        menu.popup_at_rect(a.get_window(), Gdk.Rectangle(80, 200, 1, 1), Gdk.Gravity.NORTH_WEST,
                           Gdk.Gravity.NORTH_WEST, None)
        return False

    GLib.timeout_add(500, abrir)
    a.present()
    Gtk.main()
''')


_CADASTRO = textwrap.dedent('''
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    w = Gtk.Window(title="Cadastro")
    w.set_default_size(600, 400)
    caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=20)
    nome = Gtk.Entry(); nome.get_accessible().set_name("Nome"); nome.set_text("Fulano")
    nome.connect("changed", lambda e: print("NOME", e.get_text(), flush=True))
    novidades = Gtk.CheckButton(label="Receber novidades")
    novidades.connect("toggled", lambda b: print("CAIXA", b.get_active(), flush=True))
    cidade = Gtk.ComboBoxText(); cidade.get_accessible().set_name("Cidade")
    for c in ("Recife", "Santos", "Belém"):
        cidade.append_text(c)
    cidade.connect("changed", lambda c: print("CIDADE", c.get_active_text(), flush=True))
    salvar = Gtk.Button(label="Salvar")
    salvar.connect("clicked", lambda *_: print("SALVOU", flush=True))
    for item in (nome, novidades, cidade, salvar):
        caixa.pack_start(item, False, False, 0)
    w.add(caixa)
    w.connect("destroy", Gtk.main_quit)
    w.show_all()
    w.present()
    Gtk.main()
''')


@pytest.fixture(scope="module")
def ambiente():
    """Xvfb, barramento de sessão e o de acessibilidade, uma vez por módulo."""
    import random

    numero = random.randint(300, 900)
    xvfb = subprocess.Popen(["Xvfb", f":{numero}", "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    saida = subprocess.run(["dbus-launch", "--sh-syntax"], capture_output=True, text=True, check=True).stdout
    anterior = dict(os.environ)
    for linha in saida.splitlines():
        if "=" in linha and linha.startswith("DBUS_"):
            chave, valor = linha.rstrip(";").split("=", 1)
            os.environ[chave] = valor.strip("'")
    os.environ["DISPLAY"] = f":{numero}"
    lancador = subprocess.Popen([_LANCADOR, "--launch-immediately"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    yield f":{numero}"
    for processo in (lancador, xvfb):
        processo.kill()
    pid = os.environ.get("DBUS_SESSION_BUS_PID")
    if pid:
        subprocess.run(["kill", pid], check=False)
    os.environ.clear()
    os.environ.update(anterior)


def _abrir(script, log, minimo):
    """Abre o app e espera o AT-SPI mostrar ao menos `minimo` elementos."""
    app = subprocess.Popen([sys.executable, "-c", script], stdout=open(log, "w"), stderr=subprocess.DEVNULL)
    elementos = []
    for _ in range(60):
        try:
            elementos = computador_atspi.elementos()
        except Exception:
            elementos = []
        if len(elementos) >= minimo:
            break
        time.sleep(0.25)
    return app, elementos


@pytest.fixture
def login(ambiente, tmp_path):
    app, elementos = _abrir(_JANELA, tmp_path / "app.log", 4)
    yield {"display": ambiente, "log": tmp_path / "app.log", "elementos": elementos}
    app.kill()
    app.wait()


@pytest.fixture
def tres_janelas(ambiente, tmp_path):
    app, _ = _abrir(_TRES_JANELAS, tmp_path / "app.log", 6)
    time.sleep(0.8)  # o menu abre meio segundo depois
    yield computador_atspi.elementos()
    app.kill()
    app.wait()


def test_acha_os_elementos_com_o_papel_certo(login):
    achados = filtrar(login["elementos"], (1280, 800))
    assert [(e.papel, e.nome) for e in achados] == [
        ("campo", "Usuário"), ("senha", "Senha"), ("caixa", "Lembrar de mim"), ("botao", "Entrar")]


def test_o_foco_no_campo_de_senha_aparece_como_senha(login):
    foco = computador_atspi.foco()
    assert foco is not None and foco.papel == "senha"


def test_clicar_no_centro_do_elemento_chega_ao_botao(login):
    from tato.computador.computador_x11 import BackendX11

    entrar = next(e for e in login["elementos"] if e.nome == "Entrar")
    backend = BackendX11(login["display"])
    try:
        backend.clicar(*entrar.centro)
    finally:
        backend.fechar()
    for _ in range(20):
        if "CLICOU ENTRAR" in login["log"].read_text():
            break
        time.sleep(0.1)
    assert "CLICOU ENTRAR" in login["log"].read_text()


def test_menu_aberto_primeiro_e_o_que_esta_coberto_fica_de_fora(tres_janelas):
    """Menu aberto na Janela A, que cobre parte da C; a B fica ao lado. Sem
    gerenciador de janelas, a em foco é a que contém o menu."""
    achados = [(e.janela, e.nome) for e in filtrar(tres_janelas, (1280, 800))]
    assert achados == [("menu", "Copiar"), ("menu", "Colar"), ("menu", "Excluir"),
                       ("Janela A", "Salvar"), ("Janela A", "Abrir"), ("Janela B", "Botão de B")]


@pytest.fixture
def cadastro(ambiente, tmp_path):
    app, _ = _abrir(_CADASTRO, tmp_path / "app.log", 4)
    yield {"display": ambiente, "log": tmp_path / "app.log"}
    app.kill()
    app.wait()


def test_aperta_marca_digita_e_escolhe_sem_mexer_no_mouse(cadastro):
    """`invocar` e `definir_valor` sem o mouse. O campo recebe o texto tecla
    por tecla pelo teclado de verdade do X (o GTK vê cada letra chegar), por
    cima do que havia; a lista escolhe a opção direto. A leitura depois traz
    o valor e o estado novos."""
    from Xlib import display

    from tato.computador import computador
    from tato.computador.computador_x11 import BackendX11

    raiz = display.Display(cadastro["display"]).screen().root
    antes = raiz.query_pointer()
    lidos = {e.nome: e for e in filtrar(computador_atspi.elementos(), (1280, 800))}
    assert lidos["Receber novidades"].estado == "desmarcada" and lidos["Nome"].valor == "Fulano"
    computador_atspi.invocar(lidos["Salvar"])
    computador_atspi.invocar(lidos["Receber novidades"])
    backend = BackendX11(cadastro["display"])
    try:
        for alvo, valor in ((lidos["Nome"], "Ana Souza"), (lidos["Cidade"], "Santos")):
            computador._executar_gesto(backend, "definir_valor", {"_alvo": alvo, "valor": valor},
                                       backend.tamanho())
    finally:
        backend.fechar()
    for _ in range(30):
        log = cadastro["log"].read_text()
        if "CIDADE Santos" in log:
            break
        time.sleep(0.1)
    linhas = log.splitlines()
    assert {"SALVOU", "CAIXA True", "NOME Ana Souza", "CIDADE Santos"} <= set(linhas)
    assert [linha for linha in linhas if linha.startswith("NOME A")][:3] == ["NOME A", "NOME An", "NOME Ana"], \
        "uma letra por vez, não o texto de uma vez"
    assert "NOME FulanoA" not in linhas, "o que havia foi selecionado e trocado"
    depois = {e.nome: e for e in filtrar(computador_atspi.elementos(), (1280, 800))}
    assert depois["Nome"].valor == "Ana Souza" and depois["Receber novidades"].estado == "marcada"
    assert depois["Cidade"].valor == "Santos"
    agora = raiz.query_pointer()
    assert (agora.root_x, agora.root_y) == (antes.root_x, antes.root_y), "o mouse não se moveu"
