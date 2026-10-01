"""Contratos pequenos entre o Harness e qualquer backend de navegador.

O Planner conhece a capability ``browser``; nomes e detalhes do backend ficam
atrás destes tipos. Trocar Chrome local por uma sessão remota não deve alterar
policy, planejamento nem o formato da observação.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


ACOES_IDEMPOTENTES = frozenset({"snapshot", "console", "imagens", "encerrar"})
ACOES_MUTANTES = frozenset({"clicar", "digitar", "navegar", "rolar"})
# Uma sequência de passos executada na mesma chamada.
ACAO_ROTEIRO = "roteiro"
ACOES_CONHECIDAS = ACOES_IDEMPOTENTES | ACOES_MUTANTES | {ACAO_ROTEIRO}
# Passos de roteiro que nao sao acao na pagina: devolvem ou seguram o controle.
PASSOS_DE_CONTROLE = frozenset({"esperar", "conferir", "ver", "pensar"})
PASSOS_DE_ROTEIRO = ACOES_MUTANTES | PASSOS_DE_CONTROLE
# Ações da própria ferramenta que, dentro do roteiro, têm um passo equivalente:
# `imagens` é o print (`ver`) e `snapshot` é ler a página e voltar a decidir.
PASSOS_EQUIVALENTES = {"imagens": "ver", "snapshot": "pensar"}


def tipo_do_passo(passo: Any) -> str:
    tipo = str((passo or {}).get("acao") or "").strip().lower()
    return PASSOS_EQUIVALENTES.get(tipo, tipo)
# Mudam a pagina mas nao o mundo: abrir e rolar nao comunicam nem transmitem
# nada. Confirmacao e por consequencia.
ACOES_SEM_CONSEQUENCIA = frozenset({"navegar", "rolar"})

# O que um clique ou uma digitacao causa fora da pagina, declarado pelo modelo.
CONSEQUENCIA_NENHUMA = "nenhuma"
# Consequências externas exigem confirmação a cada ocorrência.
CONSEQUENCIAS_NA_HORA = frozenset({
    "comunicar", "transmitir_dado", "apagar", "financeira", "conta", "captcha",
})
# A pessoa faz: pular aviso de seguranca, paywall, troca de senha.
CONSEQUENCIAS_DO_USUARIO = frozenset({"contornar_seguranca"})
CONSEQUENCIAS = frozenset({CONSEQUENCIA_NENHUMA}) | CONSEQUENCIAS_NA_HORA | CONSEQUENCIAS_DO_USUARIO

@dataclass(frozen=True)
class Action:
    """Uma intenção de navegador, ainda sujeita à policy."""

    tipo: str
    argumentos: Mapping[str, Any] = field(default_factory=dict)
    consequencia: str = ""

    def __post_init__(self) -> None:
        normalizado = str(self.tipo or "").strip().lower()
        if normalizado not in ACOES_CONHECIDAS:
            raise ValueError(f"ação de navegador desconhecida: {self.tipo}")
        consequencia = str(self.consequencia or "").strip().lower()
        if consequencia and consequencia not in CONSEQUENCIAS:
            raise ValueError(
                f"consequência desconhecida: {self.consequencia}; use uma de {', '.join(sorted(CONSEQUENCIAS))}"
            )
        object.__setattr__(self, "tipo", normalizado)
        object.__setattr__(self, "argumentos", dict(self.argumentos or {}))
        object.__setattr__(self, "consequencia", consequencia)

    @classmethod
    def de_argumentos(cls, arguments: Mapping[str, Any]) -> "Action":
        """A chamada da ferramenta `browser` vira Action. Um lugar so."""
        arguments = arguments or {}
        argumentos = arguments.get("argumentos") or arguments.get("arguments") or {}
        # Modelos que planejam em JSON de texto às vezes achatam os campos da
        # ação. Aceitar essa forma aqui evita transformar `url`/`ref` presentes
        # em uma ação vazia; a execução continua sujeita à mesma policy.
        if not argumentos:
            argumentos = {
                chave: valor for chave, valor in arguments.items()
                if chave not in {"acao", "action", "argumentos", "arguments", "consequencia", "provider"}
            }
        return cls(
            str(arguments.get("acao") or arguments.get("action") or ""),
            argumentos,
            str(arguments.get("consequencia") or argumentos.get("consequencia") or ""),
        )

    @property
    def idempotente(self) -> bool:
        return self.tipo in ACOES_IDEMPOTENTES

    def passos(self) -> list["Action"]:
        """Os passos de um roteiro que agem na pagina, validados.

        Passo de controle nao vira Action: ele nao tem consequencia nem
        permissao, e quem o interpreta e o executor do roteiro.
        """
        if self.tipo != ACAO_ROTEIRO:
            return []
        brutos = self.argumentos.get("passos")
        if not isinstance(brutos, list) or not brutos:
            raise ValueError("roteiro precisa de `passos`, uma lista nao vazia")
        acoes = []
        for i, passo in enumerate(brutos, start=1):
            tipo = tipo_do_passo(passo)
            if tipo not in PASSOS_DE_ROTEIRO:
                raise ValueError(f"passo {i}: '{tipo}' nao existe; use um de {', '.join(sorted(PASSOS_DE_ROTEIRO))}")
            if tipo in ACOES_MUTANTES:
                acoes.append(Action.de_argumentos(passo))
        return acoes

    @property
    def impressao(self) -> str:
        """Hash do conteudo do roteiro: aprovar um roteiro nao aprova outro."""
        import hashlib
        import json

        corpo = json.dumps(self.argumentos.get("passos") or [], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(corpo.encode("utf-8")).hexdigest()[:10]

    @property
    def consequencia_efetiva(self) -> str:
        """A declarada, exceto quando o texto digitado denuncia dado sensivel."""
        if self.tipo == ACAO_ROTEIRO:
            # A mais seria entre os passos decide a pergunta do roteiro inteiro.
            efetivas = [p.consequencia_efetiva for p in self.passos() if p.pede_autorizacao]
            for grupo in (CONSEQUENCIAS_DO_USUARIO, CONSEQUENCIAS_NA_HORA):
                achada = next((e for e in efetivas if e in grupo), None)
                if achada:
                    return achada
            return "" if efetivas else CONSEQUENCIA_NENHUMA
        # Digitar dados sensíveis gera aviso; o envio requer confirmação própria.
        return self.consequencia

    @property
    def pede_autorizacao(self) -> bool:
        if self.idempotente or self.tipo in ACOES_SEM_CONSEQUENCIA:
            return False
        # Sem declaracao pergunta: nao saber e tratado como risco.
        return self.consequencia_efetiva != CONSEQUENCIA_NENHUMA

    @property
    def so_na_hora(self) -> bool:
        return self.consequencia_efetiva in CONSEQUENCIAS_NA_HORA

    @property
    def do_usuario(self) -> bool:
        return self.consequencia_efetiva in CONSEQUENCIAS_DO_USUARIO

    @property
    def chave_de_permissao(self) -> str:
        if self.tipo == ACAO_ROTEIRO:
            return f"browser_roteiro#{self.impressao}"
        efetiva = self.consequencia_efetiva
        if efetiva and efetiva != CONSEQUENCIA_NENHUMA:
            return f"browser_{efetiva}"
        return f"browser_{self.tipo}"

    def para_dict(self) -> dict[str, Any]:
        saida = {"acao": self.tipo, "argumentos": dict(self.argumentos)}
        if self.consequencia:
            saida["consequencia"] = self.consequencia
        return saida


@dataclass(frozen=True)
class Observation:
    """O que o navegador observou depois de uma ação ou leitura."""

    fonte: str
    dados: Any
    epoch: int = 0
    confianca: float | None = None
    interativa: bool = False
    metadados: Mapping[str, Any] = field(default_factory=dict)

    def para_dict(self) -> dict[str, Any]:
        return {
            "fonte": self.fonte,
            "dados": self.dados,
            "epoch": self.epoch,
            "confianca": self.confianca,
            "interativa": self.interativa,
            "metadados": dict(self.metadados),
        }


@runtime_checkable
class BrowserCapability(Protocol):
    """Sessão ativa oferecida por qualquer provedor de navegador."""

    session_id: str

    async def execute(self, action: Action) -> Observation:
        """Executa uma ação já autorizada e devolve uma observação tipada."""


__all__ = [
    "ACOES_CONHECIDAS",
    "ACOES_IDEMPOTENTES",
    "ACOES_MUTANTES",
    "ACOES_SEM_CONSEQUENCIA",
    "ACAO_ROTEIRO",
    "PASSOS_DE_CONTROLE",
    "PASSOS_DE_ROTEIRO",
    "CONSEQUENCIAS",
    "CONSEQUENCIAS_DO_USUARIO",
    "CONSEQUENCIAS_NA_HORA",
    "Action",
    "BrowserCapability",
    "Observation",
]
