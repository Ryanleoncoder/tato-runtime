"""O texto que nunca é digitado, nem com aprovação.

A lista é curta de propósito: fica só o que destrói a máquina, toma
privilégio ou baixa código de fora para executar. O resto passa pela aprovação
normal. É uma lista de recusa: os comandos abaixo estão aqui para serem
barrados.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

# Início de comando: começo do texto, depois de `;`, `&&`, `||`, `|`, quebra de
# linha ou dentro de `$(`. Sem a âncora, a palavra dentro de uma string
# (`echo "não reboot isso"`) dispararia a recusa.
_INICIO = r"(?:^|[\n;&|]|\$\(|`)\s*"

# A raiz do disco chega como `C:\`, `C:/` ou `$env:SystemDrive`.
_RAIZ = r"(?:[a-z]:[\\/]?\s*$|\$env:systemdrive)"

_PADROES: Tuple[Tuple[str, str], ...] = (
    # baixa código de fora e executa
    (_INICIO + r"(?:iex|invoke-expression)\b", "executa texto baixado como código"),
    (r"(?i)\b(?:downloadstring|downloadfile)\s*\(", "baixa e executa conteúdo remoto"),
    (r"(?i)\b(?:curl|wget|irm|iwr)\b[^\n|]*\|\s*(?:ba)?sh\b", "canaliza download direto para o shell"),
    (r"(?i)-e(?:nc|ncodedcommand)\s+[A-Za-z0-9+/=]{16,}", "comando embaralhado em base64"),
    # toma privilégio ou afrouxa a máquina
    (r"(?i)\bset-executionpolicy\b", "afrouxa a política de execução do Windows"),
    (r"(?i)\bstart-process\b[^\n]*-verb\s+runas", "pede elevação de privilégio"),
    (_INICIO + r"sudo\s+-S\b", "tenta senha de root pela entrada padrão"),
    (r"(?i)\bnew-service\b|\bschtasks\s+/create\b", "instala serviço ou tarefa agendada"),
    (r"(?i)\bnet\s+user\b[^\n]*/add", "cria usuário no sistema"),
    # destrói a máquina
    (r"(?i)\bformat-volume\b|\bvssadmin\b|\bbcdedit\b", "mexe em disco, backup ou boot"),
    (r"(?i)\bremove-item\b[^\n]*-recurse[^\n]*-force[^\n]*" + _RAIZ, "apaga recursivamente a raiz"),
    (_INICIO + r"rm\s+(?:-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)\s+/\s*$", "apaga a raiz do sistema"),
    (r"(?i)\bmkfs(?:\.[a-z0-9]+)?\b|\bdd\b[^\n]*\bof=/dev/", "formata ou sobrescreve o disco"),
    (_INICIO + r"(?:shutdown|reboot|halt|poweroff)\b", "desliga a máquina"),
    (r"(?i)\breg\s+(?:add|delete)\b", "altera o registro do Windows"),
    (r"(?i)\bchmod\s+777\s+/\s*$", "abre permissão da raiz"),
)

_COMPILADOS = tuple((re.compile(padrao, re.IGNORECASE | re.MULTILINE), motivo)
                    for padrao, motivo in _PADROES)

# Um verbo de apagar mirando a pasta do usuário inteira ou uma pasta do sistema.
# Apagar dentro da pasta do usuário (`~/projeto/build`) passa pela aprovação.
_VERBO_DE_APAGAR = re.compile(
    r"(?:(?<![\w.-])(?:rm|rmdir|unlink|shred|srm|del|erase|rd|ri|remove-item)(?![\w-])"
    r"|\b(?:rmtree|removedirs|rmSync|rmdirSync|unlinkSync)\s*\("
    r"|\bos\.(?:remove|unlink|rmdir)\s*\("
    r"|\bfind\b(?=[^\n]*(?:-delete\b|-exec\s+(?:rm|shred)\b)))",
    re.IGNORECASE,
)
# Fim do alvo: espaço, aspas, `;`, `)`, `,`, fim de linha ou só o curinga (`/*`).
_FIM = r"(?:[\\/]?\*?)(?=[\s\"');,&|]|$)"
_ALVO_SENSIVEL = re.compile(
    r"(?:(?<![\w/.~-])~" + _FIM
    + r"|[\"']?\$\{?home\}?[\"']?" + _FIM
    + r"|%userprofile%" + _FIM
    + r"|\$env:(?:userprofile|homepath)" + _FIM
    + r"|expanduser\(\s*[\"']~[\"']\s*\)|path\.home\(\)|os\.homedir\(\)"
    + r"|(?<![\w.~-])/(?:home|users)/[^/\s\"';)]+" + _FIM
    + r"|(?<![\w.~-])/root" + _FIM
    + r"|[a-z]:\\users\\[^\\\s\"';)]+" + _FIM
    + r"|(?<![\w.~$-])/\*?(?=[\s\"');,&|]|$)"
    + r"|(?<![\w.~$-])/(?:etc|usr|bin|sbin|lib|lib32|lib64|var|boot|opt|sys|proc|dev|srv"
    + r"|system|library|applications|private)(?![\w-])"
    + r"|[a-z]:\\?\*?(?=[\s\"');,&|]|$)"
    + r"|[a-z]:\\(?:windows|program files(?: \(x86\))?|programdata)(?![\w-])"
    + r"|\$env:(?:systemroot|windir|programfiles|programdata)\b|%(?:systemroot|windir|programfiles)%)",
    re.IGNORECASE,
)


def _sem_comentarios(texto: str) -> str:
    """O que vale é o que roda: `rm -rf / # exemplo` não escapa por ter comentário."""
    linhas: List[str] = []
    for linha in (texto or "").splitlines():
        linhas.append(re.sub(r"(?<!\S)#.*$", "", linha))
    return "\n".join(linhas)


def _dentro_de_aspas(linha: str, posicao: int) -> bool:
    """`echo 'não rode rm -rf ~'` é texto. Só o verbo é olhado assim: o alvo
    entre aspas (`rm -rf "$HOME"`) continua sendo alvo."""
    antes = linha[:posicao]
    return antes.count("'") % 2 == 1 or antes.count('"') % 2 == 1


def _remocao_sensivel(corpo: str) -> Optional[str]:
    for linha in corpo.splitlines():
        for verbo in _VERBO_DE_APAGAR.finditer(linha):
            if _dentro_de_aspas(linha, verbo.start()):
                continue
            if _ALVO_SENSIVEL.search(linha, verbo.start()):
                return "apaga a pasta do usuário ou uma pasta do sistema"
    return None


def recusar(texto: str) -> Optional[str]:
    """O motivo da recusa, ou None quando nada casa."""
    corpo = _sem_comentarios(texto or "")
    for regex, motivo in _COMPILADOS:
        if regex.search(corpo):
            return motivo
    return _remocao_sensivel(corpo)


__all__ = ["recusar"]
