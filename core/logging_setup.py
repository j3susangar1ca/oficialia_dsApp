"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
core/logging_setup.py — Logging centralizado con rotación a archivo.

Antes de este módulo, todo el logging del sistema iba únicamente a stdout
(`logging.basicConfig`). Eso es invisible en el ejecutable empaquetado de
Windows: `packaging/oficialia.spec` compila con `console=True` para que la
ventana de consola muestre los logs en vivo, pero esa ventana no tiene
historial (se pierde al hacer scroll o cerrarla) y no ayuda nada si el
usuario reconstruye con `console=False` en el futuro. Este módulo agrega
un `RotatingFileHandler` persistente en la carpeta de datos (junto a la
BD y storage/), sin quitar la salida por consola.

Diseño:
    - Archivo `logs/app.log` bajo `storage_root.parent` (mismo nivel que
      `data/` y `storage/`, es decir DATOS_DIR — junto a la BD).
    - Rotación por tamaño: 10 MB por archivo, 5 respaldos (`app.log.1`...
      `app.log.5`), igual que pide la auditoría operativa.
    - Formato idéntico al de consola para no duplicar lógica de formato.
    - Nunca lanza: si el disco está lleno o la carpeta no es escribible,
      se degrada a solo-consola con una advertencia (el logging nunca debe
      tumbar el arranque de la aplicación).
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

FORMATO = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
FORMATO_FECHA = "%H:%M:%S"

#: Tamaño máximo por archivo antes de rotar (10 MB).
TAMANO_MAXIMO_BYTES = 10 * 1024 * 1024
#: Respaldos rotados a conservar (app.log.1 … app.log.5).
RESPALDOS = 5


def configurar_logging(directorio_datos: Path, *, nivel: int = logging.INFO) -> None:
    """
    Configura el logging raíz: consola (como antes) + archivo rotativo en
    `<directorio_datos>/logs/app.log`. Idempotente — seguro de llamar una
    sola vez al arranque de main.py.
    """
    raiz = logging.getLogger()
    raiz.setLevel(nivel)

    formateador = logging.Formatter(FORMATO, datefmt=FORMATO_FECHA)

    consola = logging.StreamHandler()
    consola.setFormatter(formateador)
    raiz.addHandler(consola)

    try:
        directorio_logs = directorio_datos / "logs"
        directorio_logs.mkdir(parents=True, exist_ok=True)
        archivo = logging.handlers.RotatingFileHandler(
            directorio_logs / "app.log",
            maxBytes=TAMANO_MAXIMO_BYTES,
            backupCount=RESPALDOS,
            encoding="utf-8",
        )
        archivo.setFormatter(formateador)
        raiz.addHandler(archivo)
    except OSError:
        logging.getLogger("oficialia.logging").warning(
            "No se pudo abrir el log rotativo en %s; continúa solo por consola",
            directorio_datos / "logs",
            exc_info=True,
        )

    logging.getLogger("nicegui").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
