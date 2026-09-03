"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
ui/layout.py — Encabezado, navegación, KPIs y contexto compartido de la UI.

Además de la carcasa visual (barra superior con identidad institucional,
indicador de monitoreo en vivo y capturista en turno), este módulo actúa
como punto de montaje de las dependencias compartidas por las vistas:
`main.py` inyecta aquí el pipeline y la configuración ANTES de que se
registren las páginas, evitando imports circulares.

El refresco "en vivo" del original vía WebSocket se resuelve aquí con un
`ui.timer` por página que reconsulta SQLite (local y barato) — sin capas
de transporte adicionales.
"""

from __future__ import annotations

from typing import Optional

from nicegui import ui

from config import Configuracion
from core.pipeline import FlujoDocumental

# ----------------------------------------------------------------------
# Contexto compartido (montado por main.py antes de registrar páginas)
# ----------------------------------------------------------------------
pipeline: Optional[FlujoDocumental] = None
configuracion: Optional[Configuracion] = None


def montar_contexto(flujo: FlujoDocumental, config: Configuracion) -> None:
    """Inyecta las dependencias compartidas por todas las vistas."""
    global pipeline, configuracion
    pipeline = flujo
    configuracion = config


def obtener_pipeline() -> FlujoDocumental:
    assert pipeline is not None, "El contexto de la UI no fue montado (ver main.py)"
    return pipeline


def obtener_config() -> Configuracion:
    assert configuracion is not None, "El contexto de la UI no fue montado (ver main.py)"
    return configuracion


# ----------------------------------------------------------------------
# Paleta institucional (azul 'brand' del frontend original)
# ----------------------------------------------------------------------
COLOR_PRIMARIO = "#2563eb"      # brand-600
COLOR_PRIMARIO_SUAVE = "#eff6ff"  # brand-50


def aplicar_tema() -> None:
    """Colores base de Quasar alineados a la identidad visual original."""
    ui.colors(primary=COLOR_PRIMARIO)


# ----------------------------------------------------------------------
# Encabezado
# ----------------------------------------------------------------------
def encabezado(capturista_ref: dict) -> None:
    """
    Barra superior: identidad DSA + indicador de monitoreo + capturista.

    :param capturista_ref: dict reactivo {'valor': str} compartido con las vistas.
    """
    with ui.header().classes("items-center justify-between px-4 py-2 bg-white border-b"):
        with ui.row().classes("items-center gap-2.5 no-wrap"):
            ui.label("DSA").classes(
                "inline-flex items-center justify-center rounded-lg text-sm font-bold text-white"
            ).style("width:32px;height:32px;background:#2563eb")
            with ui.column().classes("gap-0 leading-tight"):
                ui.label("Oficialía Digital").classes("text-sm font-semibold text-slate-800")
                with ui.row().classes("items-center gap-1.5 no-wrap"):
                    ui.element("div").classes("inline-block rounded-full bg-emerald-500").style(
                        "width:6px;height:6px"
                    )
                    ui.label("Monitoreo activo").classes("text-[11px] text-slate-400")
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.label("Capturista").classes("text-xs text-slate-500")
            ui.input(placeholder="CAPTURISTA-DEV").classes("w-44").props(
                'dense outlined color=primary'
            ).bind_value_from(capturista_ref, "valor").bind_value_to(capturista_ref, "valor")


# ----------------------------------------------------------------------
# Panel de KPIs
# ----------------------------------------------------------------------
def panel_kpis() -> dict[str, ui.label]:
    """
    Tarjetas de contadores: Pendientes / En proceso / Errores / Completados.
    Devuelve referencias a los labels para que la vista los actualice en vivo.
    """
    definiciones = [
        ("pendientes", "Pendientes", "#f59e0b", "bg-amber-50"),
        ("en_proceso", "En proceso", "#0284c7", "bg-sky-50"),
        ("errores", "Errores", "#e11d48", "bg-rose-50"),
        ("completados", "Completados", "#059669", "bg-emerald-50"),
    ]
    referencias: dict[str, ui.label] = {}
    with ui.row().classes("w-full gap-3 no-wrap"):
        for clave, titulo, color, fondo in definiciones:
            with ui.card().classes(f"{fondo} shadow-none rounded-lg q-pa-sm flex-1 min-w-0"):
                ui.label(titulo).classes("text-[11px] font-semibold uppercase tracking-wide text-slate-500")
                referencias[clave] = ui.label("0").classes("text-2xl font-bold").style(f"color:{color}")
    return referencias


def actualizar_kpis(referencias: dict[str, ui.label]) -> None:
    """Reconsulta SQLite y repinta los contadores."""
    repo = obtener_pipeline().repo
    kpis = repo.contadores_kpi()
    for clave, etiqueta in referencias.items():
        etiqueta.set_text(str(kpis.get(clave, 0)))


# ----------------------------------------------------------------------
# Utilidades visuales compartidas
# ----------------------------------------------------------------------
def tiempo_relativo(iso: str) -> str:
    """'hace 5 min', 'hace 2 h' para listas densas (estilo frontend original)."""
    from datetime import datetime, timezone

    try:
        momento = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "—"
    segundos = (datetime.now(timezone.utc) - momento).total_seconds()
    if segundos < 60:
        return "justo ahora"
    if segundos < 3600:
        return f"hace {int(segundos // 60)} min"
    if segundos < 86400:
        return f"hace {int(segundos // 3600)} h"
    if segundos < 86400 * 6:
        return f"hace {int(segundos // 86400)} d"
    return momento.strftime("%d %b %Y")


def tarjeta_vacia(mensaje: str) -> None:
    with ui.column().classes("w-full items-center py-10 gap-1"):
        ui.label(mensaje).classes("text-xs text-slate-400")
