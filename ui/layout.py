"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
ui/layout.py — Encabezado, navegación, KPIs y contexto compartido de la UI.

Además de la carcasa visual (barra superior con identidad institucional,
indicador de monitoreo en vivo y revisor en turno), este módulo actúa
como punto de montaje de las dependencias compartidas por las vistas:
`main.py` inyecta aquí el pipeline y la configuración ANTES de que se
registren las páginas, evitando imports circulares.

También concentra el sistema visual compartido (paleta institucional,
badges "soft pill") para que la bandeja y la revisión HITL luzcan como
una sola aplicación coherente, no dos pantallas con estilos distintos.

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


#: Valor de respaldo del revisor cuando el campo de la cabecera queda vacío
#: (identifica quién confirmó/descartó/reintentó en el historial de auditoría).
REVISOR_POR_DEFECTO = "REVISOR-DSA"


# ----------------------------------------------------------------------
# Sistema visual institucional (HCG · DSA)
# ----------------------------------------------------------------------
#: Azul institucional profundo — evita el tono "SaaS genérico" demasiado
#: saturado a favor de un acento más sobrio, apropiado para un sistema
#: de gobierno/salud.
COLOR_PRIMARIO = "#1d4ed8"
COLOR_PRIMARIO_OSCURO = "#1e3a8a"
COLOR_PRIMARIO_SUAVE = "#eff6ff"

#: Fondo general de página: gris casi blanco para que las tarjetas
#: blancas floten con una separación sutil (look "premium minimalista"
#: en vez de blanco-sobre-blanco).
COLOR_FONDO_PAGINA = "#f8fafc"

#: Paleta "soft pill" para badges de estado/método: traduce los nombres
#: de color de Quasar (fuente única en core.models.META_ESTADOS y los
#: mapas de views_dashboard) a un par (fondo claro, texto oscuro) — el
#: mismo lenguaje visual en la bandeja y en la cabecera de revisión.
_PALETA_BADGE: dict[str, tuple[str, str]] = {
    "grey-3": ("#f1f5f9", "#475569"),
    "grey-6": ("#e2e8f0", "#475569"),
    "grey-7": ("#e2e8f0", "#334155"),
    "info": ("#e0f2fe", "#0369a1"),
    "warning": ("#fef3c7", "#92400e"),
    "primary": ("#dbeafe", "#1d4ed8"),
    "negative": ("#fee2e2", "#b91c1c"),
    "positive": ("#d1fae5", "#047857"),
    "orange-8": ("#ffedd5", "#c2410c"),
}


def estilo_badge(color_quasar: str) -> str:
    """
    Traduce un nombre de color de Quasar (ej. 'warning', 'positive') al
    estilo inline 'background:…;color:…' de un badge soft-pill. Se usa en
    la tabla de la bandeja y en la barra de contexto de revisión para que
    ambas pantallas compartan exactamente el mismo lenguaje visual.
    """
    fondo, texto = _PALETA_BADGE.get(color_quasar, _PALETA_BADGE["grey-6"])
    return f"background:{fondo};color:{texto}"


def aplicar_tema() -> None:
    """Colores base de Quasar + fondo de página, alineados a la identidad institucional."""
    ui.colors(primary=COLOR_PRIMARIO)
    ui.query("body").style(f"background:{COLOR_FONDO_PAGINA}")


# ----------------------------------------------------------------------
# Encabezado
# ----------------------------------------------------------------------
def encabezado(revisor_ref: dict) -> None:
    """
    Barra superior: identidad institucional (HCG · Oficialía Digital) +
    indicador de monitoreo en vivo + campo del revisor en turno.

    :param revisor_ref: dict reactivo {'valor': str} compartido con las vistas.
    """
    with ui.header().classes(
        "items-center justify-between px-5 py-2.5 bg-white no-wrap"
    ).style("box-shadow:0 1px 3px rgba(15,23,42,.06);border-bottom:1px solid #e2e8f0"):
        with ui.row().classes("items-center gap-3 no-wrap"):
            # Monograma institucional: si más adelante se cuenta con el
            # isotipo oficial de HCG, esta marca de texto se reemplaza por
            # un <img> sin tocar el resto del encabezado.
            ui.label("HCG").classes(
                "inline-flex items-center justify-center rounded-xl text-[13px] "
                "font-bold text-white tracking-wide select-none"
            ).style(
                "width:38px;height:38px;flex:none;"
                f"background:linear-gradient(135deg,{COLOR_PRIMARIO_OSCURO},{COLOR_PRIMARIO});"
                "box-shadow:0 2px 6px rgba(29,78,216,.35)"
            )
            with ui.column().classes("gap-0 leading-tight"):
                ui.label("Oficialía Digital").classes(
                    "text-[15px] font-semibold text-slate-800 tracking-tight"
                )
                ui.label("Hospital Civil de Guadalajara · DSA").classes(
                    "text-[11px] text-slate-400 font-medium"
                )

        with ui.row().classes("items-center gap-4 no-wrap"):
            with ui.row().classes("items-center gap-1.5 no-wrap"):
                ui.element("div").classes("inline-block rounded-full bg-emerald-500").style(
                    "width:6px;height:6px;box-shadow:0 0 0 3px rgba(16,185,129,.15)"
                )
                ui.label("En línea").classes("text-[11px] text-slate-400 font-medium")

            ui.element("div").style("width:1px;height:22px;background:#e2e8f0")

            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon("badge", size="18px").classes("text-slate-400")
                ui.input(placeholder="Nombre del revisor").classes("w-44").props(
                    "dense outlined color=primary"
                ).bind_value_from(revisor_ref, "valor").bind_value_to(revisor_ref, "valor")


# ----------------------------------------------------------------------
# Panel de KPIs
# ----------------------------------------------------------------------
def panel_kpis() -> dict[str, ui.label]:
    """
    Tarjetas de contadores: Pendientes / En proceso / Errores / Completados.
    Devuelve referencias a los labels para que la vista los actualice en vivo.

    Estilo "tarjeta neutra + acento de color" (borde izquierdo) en vez de
    fondos saturados: más legible en conjunto y más acorde al resto del
    sistema visual sobrio.
    """
    definiciones = [
        ("pendientes", "Pendientes", "#d97706"),
        ("en_proceso", "En proceso", "#0284c7"),
        ("errores", "Errores", "#e11d48"),
        ("completados", "Completados", "#059669"),
    ]
    referencias: dict[str, ui.label] = {}
    with ui.row().classes("w-full gap-3 no-wrap"):
        for clave, titulo, color in definiciones:
            with ui.card().classes(
                "bg-white shadow-none border border-slate-200 rounded-xl q-pa-md flex-1 min-w-0"
            ).style(f"border-left:3px solid {color}"):
                ui.label(titulo).classes(
                    "text-[10.5px] font-semibold uppercase tracking-wider text-slate-400"
                )
                referencias[clave] = ui.label("0").classes(
                    "text-[26px] font-bold text-slate-800 leading-tight"
                )
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
        ui.icon("inbox", size="28px").classes("text-slate-300")
        ui.label(mensaje).classes("text-xs text-slate-400")
