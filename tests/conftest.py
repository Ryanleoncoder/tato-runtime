"""Cada teste usa uma pasta do Tato própria: um Tato ligado na máquina segura a
trava real do computador, e os testes de gesto não podem esbarrar nela."""
import pytest


@pytest.fixture(autouse=True)
def _pasta_do_tato_isolada(tmp_path, monkeypatch):
    monkeypatch.setenv("TATO_HOME", str(tmp_path / "tato-home"))
