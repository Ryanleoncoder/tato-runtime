"""Os códigos de erro que acompanham uma recusa. O agente lê a frase; o código
é para quem trata a recusa por programa."""


class CodigoDeErro:
    RECUSADO_POLITICA = "EXEC_RECUSADO_POLITICA"
    PRECISA_PERMISSAO = "EXEC_PRECISA_PERMISSAO"
    ALVO_SENSIVEL = "EXEC_ALVO_SENSIVEL"
    TIMEOUT = "EXEC_TIMEOUT"
    FALHA_INFRA = "EXEC_FALHA_INFRA"


__all__ = ["CodigoDeErro"]
