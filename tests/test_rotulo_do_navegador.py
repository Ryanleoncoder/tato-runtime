"""O texto do balão sobre a aba: o campo aparece, o que é digitado não."""
from tato.navegador import browser_sobreposicao as sob


def test_digitar_mostra_o_campo_e_nunca_o_texto():
    texto = sob.rotulo("digitar", {"ref": "e1", "texto": "minha-senha-123"}, "Senha")
    assert texto == "digitar em “Senha”" and "minha-senha" not in texto


def test_navegar_mostra_so_o_site():
    assert sob.rotulo("navegar", {"url": "https://vagas.exemplo.com.br/busca?q=x"}) == \
        "navegar · vagas.exemplo.com.br"


def test_nome_comprido_encurta():
    assert sob.rotulo("clicar", {}, "x" * 100).endswith("…”")
