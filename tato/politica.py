"""O que pode rodar, decidido antes de qualquer gesto.

Duas camadas. O `portao` olha a chamada: ação conhecida, destino público,
URL sem credencial, e o que só a pessoa pode fazer. O `avaliar` olha o que
vai ser executado: primeiro as travas que não se aprovam (`hardline` e alvo
com credencial), depois as aprovações da sessão (`permissoes`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import hardline, permissoes

# Nomes de arquivo e pasta que guardam credencial: texto que os menciona não é digitado.
ALVOS_PROIBIDOS = (".env", "auth.json", "cofre.json", "permissoes.json", "credentials", "id_rsa",
                   "id_ed25519", ".pem", ".pfx", ".p12", "secrets", "shadow")
PASTAS_PROIBIDAS = (".ssh", ".aws", ".gnupg")


def alvo_sensivel(texto: str) -> Optional[str]:
    baixo = str(texto or "").lower()
    if not baixo:
        return None
    for alvo in ALVOS_PROIBIDOS:
        if alvo in baixo:
            return f"o texto menciona '{alvo}', que guarda credencial"
    for pasta in PASTAS_PROIBIDAS:
        if f"/{pasta}/" in baixo or "\\" + pasta + "\\" in baixo:
            return f"o texto alcança a pasta '{pasta}', que guarda credencial"
    return None


def recusa_sem_apelacao(texto: str, *, checar_alvo: bool = True) -> Optional[str]:
    """O motivo de nunca rodar, ou None."""
    motivo = hardline.recusar(texto)
    if motivo:
        return f"recusado sem apelação: {motivo}"
    return alvo_sensivel(texto) if checar_alvo else None


def avaliar(comando: str, session_id: str = "", *, corpo: str = "", checar_alvo: bool = True,
            na_hora: bool = False) -> Dict[str, Any]:
    """Se isto pode rodar agora. Recusa sem apelação não vira pergunta."""
    for texto in (comando, corpo):
        motivo = recusa_sem_apelacao(texto, checar_alvo=checar_alvo) if texto else None
        if motivo:
            return {"permitido": False, "precisa_perguntar": False, "sem_apelacao": True,
                    "chave": "", "motivo": motivo}
    decisao = dict(permissoes.avaliar(comando, session_id, na_hora=na_hora))
    decisao["sem_apelacao"] = False
    return decisao


@dataclass
class Decisao:
    allowed: bool
    reason: str
    missing: List[str] = field(default_factory=list)


class Portao:
    """A primeira checagem de cada chamada, antes do despacho."""

    def evaluate(self, state: Any, tool: str, user_message: str = "",
                 arguments: Optional[Dict[str, Any]] = None) -> Decisao:
        if tool == "computador":
            return Decisao(True, "gestos pedem aprovação no despacho")
        if tool == "gravacao":
            return Decisao(True, "iniciar exige aprovação específica no MCP")
        if tool == "browser":
            return self._navegador(arguments or {})
        return Decisao(False, f"ferramenta desconhecida: {tool}", ["ferramenta_valida"])

    def _navegador(self, argumentos: Dict[str, Any]) -> Decisao:
        from .navegador.browser_contracts import Action
        from .navegador.web import UrlRecusada, validar_url
        from .segredos import tem_segredo

        try:
            action = Action.de_argumentos(argumentos)
            acoes = [action] + action.passos()
        except ValueError as exc:
            return Decisao(False, str(exc), ["acao_de_browser_valida"])
        if action.do_usuario:
            return Decisao(False, "isso a pessoa faz: contornar aviso de segurança, paywall ou troca de senha",
                           ["peça para a pessoa fazer este passo"])
        for passo in (a for a in acoes if a.tipo == "navegar"):
            url = str(passo.argumentos.get("url") or "")
            try:
                validar_url(url)
            except UrlRecusada as exc:
                return Decisao(False, f"navegar recusado: {exc}", ["url pública em http ou https"])
            # Página com injeção pode mandar abrir `site/?key=sk-...`: a credencial sairia na requisição.
            if tem_segredo(url):
                return Decisao(False, "navegar recusado: a URL carrega o que parece credencial",
                               ["url sem chave, token ou senha"])
        if action.idempotente:
            return Decisao(True, f"browser.{action.tipo} apenas observa a página")
        if not action.pede_autorizacao:
            return Decisao(True, f"browser.{action.tipo} muda a página, não o mundo")
        return Decisao(True, f"browser.{action.tipo} segue para a autorização")


portao = Portao()

__all__ = ["Decisao", "Portao", "alvo_sensivel", "avaliar", "portao", "recusa_sem_apelacao"]
