"""Grava e monta GIFs; a lógica compartilhada também serve ao MCP."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tato.gravacao import main  # noqa: E402


if __name__ == "__main__":
    main()
