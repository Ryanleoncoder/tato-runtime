"""Credencial, CPF e cartão num texto.

Serve para duas coisas: recusar URL que carrega credencial e avisar, sem
bloquear, quando o agente vai digitar algo sensível. CPF e cartão só contam
com dígito verificador válido; e-mail e telefone ficam de fora, porque avisar
a cada um seria o aviso que ninguém lê.
"""
from __future__ import annotations

import re
from typing import List
from urllib.parse import unquote

# A forma "nome que soa secreto = valor" e os prefixos que os provedores publicaram.
_SEGREDOS = (
    re.compile(r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)\s*[=:]\s*(\S+)"),
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})"),
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._\-]{16,})"),
)
_CPF = re.compile(r"(?<!\d)(\d{3})\.?(\d{3})\.?(\d{3})-?(\d{2})(?!\d)")
_CARTAO = re.compile(r"(?<!\d)(?:\d{4}[ -]?){3}\d{4}(?!\d)")

ROTULOS = {
    "credencial": "uma chave ou senha",
    "cpf": "um CPF",
    "cartao": "um número de cartão",
}


def tem_segredo(texto: str) -> bool:
    """Se algo no texto parece credencial, também na forma decodificada de URL
    (`sk%2Dant...` esconderia o prefixo)."""
    bruto = texto or ""
    return any(padrao.search(t) for t in (bruto, unquote(bruto)) for padrao in _SEGREDOS)


def _cpf_valido(digitos: str) -> bool:
    if len(digitos) != 11 or len(set(digitos)) == 1:
        return False
    for tamanho in (9, 10):
        soma = sum(int(d) * (tamanho + 1 - i) for i, d in enumerate(digitos[:tamanho]))
        if (soma * 10 % 11) % 10 != int(digitos[tamanho]):
            return False
    return True


def _luhn(digitos: str) -> bool:
    total = 0
    for i, d in enumerate(reversed(digitos)):
        n = int(d)
        if i % 2 == 1:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def detectar(texto: str) -> List[str]:
    """Os tipos de dado sensível no texto, na ordem de `ROTULOS`."""
    bruto = str(texto or "")
    achados = []
    if tem_segredo(bruto):
        achados.append("credencial")
    if any(_cpf_valido("".join(m.groups())) for m in _CPF.finditer(bruto)):
        achados.append("cpf")
    if any(_luhn(re.sub(r"\D", "", m.group(0))) for m in _CARTAO.finditer(bruto)):
        achados.append("cartao")
    return achados


__all__ = ["ROTULOS", "detectar", "tem_segredo"]
