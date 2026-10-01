"""Que programas estão rodando e quais estão instalados, no Windows.

`janelas` só vê o que tem janela visível. Para decidir entre trazer um app para
a frente e abri-lo, o agente precisa saber se o programa roda (mesmo minimizado
ou só na bandeja) e se está instalado. Instalado aqui é ter atalho no menu
Iniciar: é por ele que `abrir` abre, e nunca por um caminho qualquer.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import time
import unicodedata
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List, Optional

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x2
_INVALIDO = ctypes.c_void_p(-1).value
# Atalho de desinstalar, ajuda e site não são o programa.
_NAO_E_O_PROGRAMA = ("uninstall", "desinstalar", "readme", "leia-me", "help", "ajuda", "website", "site")


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                             ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    return k


def _normal(texto: str) -> str:
    """Normaliza acentos e caixa para comparar nomes de programas."""
    sem_acento = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in sem_acento if not unicodedata.combining(c)).casefold().strip()


# A área de trabalho e a barra são janelas visíveis, mas não são programa aberto.
_CLASSES_DO_SHELL = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
DWMWA_CLOAKED = 14


def escondida_pelo_sistema(hwnd) -> bool:
    """Janela que o Windows mantém "visível" sem mostrar (cloaked: host de entrada
    de texto, shell, a cópia interna de um app da Loja) ou que é o próprio shell."""
    user32 = ctypes.windll.user32
    classe = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, classe, 256)
    if classe.value in _CLASSES_DO_SHELL:
        return True
    escondida = ctypes.c_int(0)
    try:
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(escondida),
                                                   ctypes.sizeof(escondida))
    except OSError:
        return False
    return bool(escondida.value)


def executavel_do_processo(pid: int) -> str:
    """`Code.exe` do processo, ou "" quando o sistema não deixa ler."""
    k = _kernel32()
    alca = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not alca:
        return ""
    try:
        tamanho = wintypes.DWORD(1024)
        caminho = ctypes.create_unicode_buffer(tamanho.value)
        if not k.QueryFullProcessImageNameW(alca, 0, caminho, ctypes.byref(tamanho)):
            return ""
        return os.path.basename(caminho.value)
    finally:
        k.CloseHandle(alca)


def processos() -> Dict[int, str]:
    """Todos os processos: pid → executável."""
    k = _kernel32()
    foto = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not foto or foto == _INVALIDO:
        return {}
    saida: Dict[int, str] = {}
    try:
        entrada = _PROCESSENTRY32W()
        entrada.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        continua = k.Process32FirstW(foto, ctypes.byref(entrada))
        while continua:
            saida[int(entrada.th32ProcessID)] = entrada.szExeFile
            continua = k.Process32NextW(foto, ctypes.byref(entrada))
    finally:
        k.CloseHandle(foto)
    return saida


def pastas_do_menu_iniciar() -> List[Path]:
    pastas = []
    for variavel in ("ProgramData", "APPDATA"):
        raiz = os.environ.get(variavel)
        if raiz:
            pastas.append(Path(raiz) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [p for p in pastas if p.is_dir()]


_APPS: Dict[str, object] = {"quando": 0.0, "nomes": []}
_APPS_VALEM_S = 300.0


def apps_do_menu() -> List[str]:
    """Todos os apps do menu Iniciar, inclusive os da Loja (Bloco de notas,
    Calculadora), que não têm atalho nas pastas, como `nome|identificador`: o
    identificador (`Microsoft.WindowsNotepad_...`) acha o app pelo nome em
    inglês num Windows em português. A consulta custa ~1 s: fica guardada
    alguns minutos."""
    if time.monotonic() - float(_APPS["quando"]) < _APPS_VALEM_S:
        return list(_APPS["nomes"])  # type: ignore[arg-type]
    try:
        saida = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    nomes = [linha.strip() for linha in saida.splitlines() if linha.strip()]
    _APPS.update(quando=time.monotonic(), nomes=nomes)
    return nomes


def instalados(nome: str, pastas: Optional[List[Path]] = None,
               apps: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """Os programas do menu Iniciar cujo nome contém `nome`, o de nome igual
    primeiro: os atalhos das pastas e, sem `pastas` dadas, os apps da Loja."""
    procurado = _normal(nome)
    if not procurado:
        return []
    achados: Dict[str, str] = {}
    for pasta in pastas if pastas is not None else pastas_do_menu_iniciar():
        for atalho in pasta.rglob("*.lnk"):
            titulo = atalho.stem
            chave = _normal(titulo)
            if procurado in chave and not any(p in chave for p in _NAO_E_O_PROGRAMA):
                achados.setdefault(titulo, str(atalho))
    for app in apps if apps is not None else (apps_do_menu() if pastas is None else []):
        titulo, _, identificador = app.partition("|")
        chave = _normal(titulo)
        if (procurado in chave or procurado in _normal(identificador))                 and not any(p in chave for p in _NAO_E_O_PROGRAMA):
            achados.setdefault(titulo, "menu Iniciar")
    return [{"nome": n, "atalho": achados[n]}
            for n in sorted(achados, key=lambda n: (_normal(n) != procurado, len(n)))]


def janela_nova_do_atalho(nome: str, antes: List[str], agora: List[str]) -> str:
    """Atalho com nome próprio abre janela com só parte dele: "Chrome do Agente"
    vira a janela "Agente". Vale a janela que não existia antes e cujo título
    está no nome do atalho."""
    chave = _normal(nome)
    for titulo in agora:
        normal = _normal(titulo)
        if titulo not in antes and len(normal) >= 3 and normal in chave:
            return titulo
    return ""


def abrir_pelo_menu(nome: str, apertar, escrever, janela_do_programa, esperar, focar,
                    pastas: Optional[List[Path]] = None, prazo: float = 30.0,
                    interromper=lambda: False) -> str:
    """Abre como a pessoa abre: tecla Win, o nome digitado, Enter. Quem abre é o
    Windows, com o ambiente dele: aberto por um processo filho do VS Code, um app
    Electron herdava `ELECTRON_RUN_AS_NODE` e saía na hora. Depois espera a janela
    do programa aparecer e a traz para a frente. Devolve o que foi feito."""
    candidatos = instalados(nome, pastas)
    if not candidatos:
        raise LookupError(f"nenhum programa com «{nome}» no menu Iniciar; confira o nome em `programas`")
    escolhido = candidatos[0]["nome"]
    apertar("win")
    esperar(0.8)
    escrever(escolhido)
    esperar(1.0)
    apertar("enter")
    passou = 0.0
    while passou < prazo and not interromper():
        esperar(1.0)
        passou += 1.0
        titulo = janela_do_programa(escolhido)
        if titulo:
            focar(titulo)
            return f"«{escolhido}» aberto, janela «{titulo}» na frente"
    return (f"«{escolhido}» foi aberto pelo menu Iniciar, mas a janela não apareceu em {prazo:g} s; "
            "confira em `programas` antes de abrir de novo")


__all__ = ["abrir_pelo_menu", "apps_do_menu", "janela_nova_do_atalho", "executavel_do_processo", "instalados", "pastas_do_menu_iniciar", "processos"]
