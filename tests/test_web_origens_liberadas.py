"""Um site local de teste passa só quando o operador libera a origem exata."""
import pytest

from tato.navegador.web import UrlRecusada, validar_url


def test_loopback_continua_recusado_sem_liberacao(monkeypatch):
    monkeypatch.delenv("TATO_ORIGENS_LOCAIS_LIBERADAS", raising=False)
    with pytest.raises(UrlRecusada):
        validar_url("http://127.0.0.1:8765/")


def test_so_a_origem_exata_passa(monkeypatch):
    monkeypatch.setenv("TATO_ORIGENS_LOCAIS_LIBERADAS", "http://127.0.0.1:8765/")
    assert validar_url("http://127.0.0.1:8765/galeria?x=1")
    for outra in ("http://127.0.0.1:8766/", "https://127.0.0.1:8765/", "http://localhost:8765/",
                  "http://169.254.169.254/latest/meta-data"):
        with pytest.raises(UrlRecusada):
            validar_url(outra)
