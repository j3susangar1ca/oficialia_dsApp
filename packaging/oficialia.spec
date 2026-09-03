# -*- mode: python ; coding: utf-8 -*-
"""
packaging/oficialia.spec — Spec de PyInstaller para el instalador de Windows.

Genera un bundle "onedir" (una carpeta con el .exe y sus dependencias, no
un único archivo): arranca más rápido que --onefile y da menos falsos
positivos de antivirus (--onefile se auto-extrae a una carpeta temporal en
cada arranque, un patrón que muchos antivirus asocian con droppers). El
instalador de Inno Setup (packaging/oficialia.iss) empaqueta esta carpeta
completa tal cual.

Uso (normalmente invocado por packaging/build_windows.ps1, no a mano):
    pyinstaller packaging/oficialia.spec --noconfirm --distpath dist

Nota: SOLO produce un ejecutable de Windows si se ejecuta EN Windows —
PyInstaller no hace compilación cruzada. build_windows.ps1 y el workflow
de GitHub Actions (.github/workflows/build-windows-installer.yml) corren
en un runner Windows precisamente por esto.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all

# SPECPATH lo inyecta PyInstaller: carpeta que contiene este .spec.
RAIZ = Path(SPECPATH).resolve().parent  # noqa: F821 — SPECPATH es una global de PyInstaller

# ----------------------------------------------------------------------
# Paquetes con importaciones dinámicas/recursos de datos que el análisis
# estático de PyInstaller suele pasar por alto si no se recolectan de
# forma exhaustiva (módulos + datos + binarios). Ser generoso aquí es
# deliberado: preferimos un bundle algo más grande a un
# "ModuleNotFoundError" en la máquina del usuario final, donde no hay
# forma de iterar rápido.
# ----------------------------------------------------------------------
PAQUETES_COMPLETOS = [
    "nicegui",          # interfaz (incluye sus assets estáticos Vue/Quasar)
    "uvicorn",          # servidor ASGI — carga protocolos por importlib dinámico
    "starlette",
    "fastapi",
    "engineio",
    "socketio",
    "PIL",              # Pillow — realce de imagen (core/pdf_engine.py)
    "pymupdf",           # motor PDF (fitz)
    "google.genai",      # SDK de Gemini
    "google.auth",       # credenciales (Sheets + genai)
    "google.api_core",
    "gspread",           # Google Sheets
    "pydantic",
    "pydantic_core",
    "watchdog",          # vigilancia de storage/01_entrada
]

datas = [(str(RAIZ / ".env.example"), ".")]
binaries = []
hiddenimports = [
    # uvicorn resuelve estos módulos por nombre de cadena (importlib) según
    # el sistema operativo/dependencias instaladas; el análisis estático de
    # PyInstaller no los detecta como "usados" y hay que declararlos a mano.
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

for _paquete in PAQUETES_COMPLETOS:
    _d, _b, _h = collect_all(_paquete)
    datas += _d
    binaries += _b
    hiddenimports += _h

a = Analysis(  # noqa: F821 — símbolo inyectado por el runtime de PyInstaller
    [str(RAIZ / "main.py")],
    pathex=[str(RAIZ)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy.testing"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OficialiaDigitalDSA",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,   # ventana visible con los logs de arranque (transparencia operativa)
    icon=None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="OficialiaDigitalDSA",
)
