"""
tests/conftest.py — Fixtures compartidas de la suite de pytest.

Cada fixture crea su propia carpeta temporal aislada (vía `tmp_path`, que
pytest limpia solo): ninguna prueba toca `storage/`, `data/` ni `.env` del
repositorio. La suite no requiere GEMINI_API_KEY, Playwright ni Tesseract:
lo que ejercita necesita IA real usa dobles (fakes) explícitos por prueba.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config import Configuracion
from core.file_manager import GestorArchivos
from database import RepositorioDocumentos


@pytest.fixture
def configuracion(tmp_path: Path) -> Configuracion:
    """Configuración con storage/BD aislados en una carpeta temporal."""
    return Configuracion(
        storage_root=tmp_path / "storage",
        database_path=tmp_path / "data" / "oficialia.db",
    )


@pytest.fixture
def repositorio(configuracion: Configuracion) -> RepositorioDocumentos:
    """Repositorio SQLite ya inicializado (esquema + migraciones aplicadas)."""
    repo = RepositorioDocumentos(configuracion)
    repo.inicializar()
    return repo


@pytest.fixture
def gestor_archivos(configuracion: Configuracion) -> GestorArchivos:
    """Árbol storage/01_entrada…04_errores ya creado."""
    archivos = GestorArchivos(configuracion.storage_root)
    archivos.asegurar_estructura()
    return archivos
