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
from core.models import EstadoDocumento
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


def tono_badge(color_quasar: str) -> tuple[str, str]:
    """Par (fondo claro, texto oscuro) de un tono de badge — fuente única para
    `estilo_badge` y para las tarjetas KPI/filtro, de modo que ambas piezas
    compartan exactamente el mismo lenguaje de color "soft pill"."""
    return _PALETA_BADGE.get(color_quasar, _PALETA_BADGE["grey-6"])


def estilo_badge(color_quasar: str) -> str:
    """
    Traduce un nombre de color de Quasar (ej. 'warning', 'positive') al
    estilo inline 'background:…;color:…' de un badge soft-pill. Se usa en
    la tabla de la bandeja y en la barra de contexto de revisión para que
    ambas pantallas compartan exactamente el mismo lenguaje visual.
    """
    fondo, texto = tono_badge(color_quasar)
    return f"background:{fondo};color:{texto}"


def aplicar_tema() -> None:
    """Colores base de Quasar + fondo de página, alineados a la identidad institucional."""
    ui.colors(primary=COLOR_PRIMARIO)
    ui.query("body").style(f"background:{COLOR_FONDO_PAGINA}")


# ----------------------------------------------------------------------
# Animaciones compartidas (progreso, pulso "en curso", insignia de alerta)
# ----------------------------------------------------------------------
# Una sola declaración global de keyframes/clases, reutilizada por el
# stepper de fases (más abajo) Y por la barra de progreso animada que
# ui.views_dashboard dibuja bajo el badge de estado de cada fila "en
# progreso" (ver core.models.MetaEstado.en_progreso) — mismo lenguaje
# visual de "esto sigue trabajando solo" en toda la app.
# OJO `shared=True`: igual que en ui.views_dashboard, este módulo se
# importa una sola vez al arrancar (antes de que exista ningún cliente
# conectado), así que sin `shared=True` el <style> nunca llega al <head>
# real de ninguna página.
ui.add_head_html(
    """
    <style>
      @keyframes oficialia-shimmer {
        0%   { transform: translateX(-120%); }
        100% { transform: translateX(220%); }
      }
      @keyframes oficialia-pulso-anillo {
        0%   { box-shadow: 0 0 0 0 rgba(2,132,199,.32); }
        70%  { box-shadow: 0 0 0 7px rgba(2,132,199,0); }
        100% { box-shadow: 0 0 0 0 rgba(2,132,199,0); }
      }
      @keyframes oficialia-pulso-anillo-alerta {
        0%   { box-shadow: 0 0 0 0 rgba(225,29,72,.35); }
        70%  { box-shadow: 0 0 0 7px rgba(225,29,72,0); }
        100% { box-shadow: 0 0 0 0 rgba(225,29,72,0); }
      }
      /* Barra fina "aún trabajando" bajo un badge de estado. */
      .oficialia-barra-progreso {
        position: relative; overflow: hidden; height: 3px; width: 100%;
        border-radius: 3px; background: #e0f2fe; margin-top: 4px;
      }
      .oficialia-barra-progreso::after {
        content: ""; position: absolute; inset: 0; width: 35%;
        background: linear-gradient(90deg, transparent, #0284c7, transparent);
        animation: oficialia-shimmer 1.35s ease-in-out infinite;
      }
      /* Nodo del stepper de fases (ui.layout.stepper_pipeline). */
      .oficialia-paso-circulo {
        width: 32px; height: 32px; border-radius: 999px; flex: none;
        display: flex; align-items: center; justify-content: center;
        border: 2px solid #e2e8f0; background: #fff; position: relative;
      }
      .oficialia-paso-actual { animation: oficialia-pulso-anillo 1.9s ease-out infinite; }
      .oficialia-paso-alerta { animation: oficialia-pulso-anillo-alerta 1.6s ease-out infinite; }
      /* Insignia flotante "!" — el mismo lenguaje visual para el nodo del
         stepper en ERROR_RPA y para cualquier otro contador que necesite
         gritar "esto necesita que lo veas". */
      .oficialia-exclamacion {
        position: absolute; top: -5px; right: -5px; width: 16px; height: 16px;
        border-radius: 999px; background: #e11d48; color: #fff;
        font-size: 11px; font-weight: 800; line-height: 16px; text-align: center;
        box-shadow: 0 0 0 2px #fff; animation: oficialia-pulso-anillo-alerta 1.6s ease-out infinite;
      }
    </style>
    """,
    shared=True,
)


# ----------------------------------------------------------------------
# Stepper de fases del pipeline — "¿en qué parte del proceso está esto?"
# ----------------------------------------------------------------------
#: Camino feliz de 6 fases (estado, etiqueta corta, ícono Material). El
#: orden importa: define qué nodos se pintan "hechos" (a la izquierda del
#: actual) vs. "pendientes" (a la derecha).
_PASOS_STEPPER: list[tuple[EstadoDocumento, str, str]] = [
    (EstadoDocumento.INGESTADO, "Recibido", "inbox"),
    (EstadoDocumento.EN_PREPROCESO, "Preparando", "auto_fix_high"),
    (EstadoDocumento.EXTRAYENDO, "Extrayendo datos", "manage_search"),
    (EstadoDocumento.PENDIENTE_REVISION, "Por revisar", "fact_check"),
    (EstadoDocumento.EJECUTANDO_RPA, "Registrando", "cloud_upload"),
    (EstadoDocumento.COMPLETADO, "Completado", "task_alt"),
]


def stepper_pipeline(estado_actual: EstadoDocumento) -> None:
    """
    Barra horizontal de 6 fases: de un vistazo, dónde está este documento
    en el proceso y qué falta — para que el revisor nunca tenga que
    adivinar "¿esto en qué paso va?" mirando solo un badge de texto.

    DESCARTADO es una salida FUERA de este camino (el banner de
    `_banner_estado` ya lo explica con su motivo) — no dibuja stepper.
    ERROR_RPA se dibuja sobre el nodo "Registrando" con una insignia "!"
    flotante y un pulso rojo en vez del anillo azul de progreso normal:
    sigue siendo esa misma fase, solo que quedó pausada esperando
    atención humana antes de continuar (ver ui.views_hitl._banner_estado
    y la acción "Confirmar registro manual").
    """
    if estado_actual == EstadoDocumento.DESCARTADO:
        return

    es_error = estado_actual == EstadoDocumento.ERROR_RPA
    # COMPLETADO es terminal: nada sigue "en curso", así que el último nodo
    # se pinta como hecho (check verde), no como el anillo azul pulsante de
    # "actual" — ese pulso significa "esto sigue trabajando o te espera a
    # ti", y para un documento ya completado sería un falso "aún falta algo".
    terminado = estado_actual == EstadoDocumento.COMPLETADO
    if es_error:
        indice_actual = next(
            i for i, (e, _, _) in enumerate(_PASOS_STEPPER) if e == EstadoDocumento.EJECUTANDO_RPA
        )
    else:
        indice_actual = next(
            (i for i, (e, _, _) in enumerate(_PASOS_STEPPER) if e == estado_actual),
            len(_PASOS_STEPPER) - 1,
        )

    piezas: list[str] = []
    for i, (_estado, etiqueta, icono) in enumerate(_PASOS_STEPPER):
        hecho = i < indice_actual or (terminado and i == indice_actual)
        actual = i == indice_actual and not terminado

        if actual and es_error:
            clase = "oficialia-paso-circulo oficialia-paso-alerta"
            estilo = "border-color:#fb7185;background:#fff1f2;color:#e11d48"
            insignia = '<span class="oficialia-exclamacion" aria-hidden="true">!</span>'
            icono_nodo = icono
        elif actual:
            clase = "oficialia-paso-circulo oficialia-paso-actual"
            estilo = "border-color:#38bdf8;background:#f0f9ff;color:#0284c7"
            insignia = ""
            icono_nodo = icono
        elif hecho:
            clase = "oficialia-paso-circulo"
            estilo = "border-color:#10b981;background:#10b981;color:#fff"
            insignia = ""
            icono_nodo = "check"
        else:
            clase = "oficialia-paso-circulo"
            estilo = "border-color:#e2e8f0;background:#fff;color:#cbd5e1"
            insignia = ""
            icono_nodo = icono

        color_texto = "#0f172a" if (actual or hecho) else "#94a3b8"
        peso_texto = "700" if actual else "500"

        piezas.append(
            f'<div style="display:flex;flex-direction:column;align-items:center;gap:5px;'
            f'flex:0 0 auto;min-width:78px;">'
            f'<div class="{clase}" style="{estilo}">'
            f'<span class="material-icons" style="font-size:16px;" aria-hidden="true">{icono_nodo}</span>'
            f"{insignia}"
            f"</div>"
            f'<span style="font-size:10.5px;font-weight:{peso_texto};color:{color_texto};'
            f'text-align:center;white-space:nowrap;">{etiqueta}</span>'
            f"</div>"
        )
        if i < len(_PASOS_STEPPER) - 1:
            color_linea = "#10b981" if i < indice_actual else "#e2e8f0"
            piezas.append(
                f'<div style="flex:1;height:2px;background:{color_linea};'
                f'margin-top:16px;min-width:10px;"></div>'
            )

    ui.html(
        '<div style="display:flex;align-items:flex-start;width:100%;padding:4px 6px 0;">'
        + "".join(piezas)
        + "</div>"
    ).classes("w-full")


# ----------------------------------------------------------------------
# Encabezado
# ----------------------------------------------------------------------
def encabezado(revisor_ref: dict) -> None:
    """
    Barra superior: identidad institucional (HCG · Oficialía Digital) +
    indicador de monitoreo en vivo + campo del revisor en turno.

    :param revisor_ref: dict reactivo {'valor': str} compartido con las vistas
        — en la práctica siempre `app.storage.user` (persiste por navegador
        entre páginas), nunca un dict local; el tipado se deja genérico
        porque cualquier mapeo con esa forma funciona (p. ej. en pruebas).
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
                # text-slate-400 (~2.5:1 sobre blanco) incumplía WCAG AA para
                # texto pequeño; slate-600 (~7:1) cumple AAA sin perder la
                # jerarquía tenue respecto del título.
                ui.label("Hospital Civil de Guadalajara · DSA").classes(
                    "text-[11px] text-slate-600 font-medium"
                )

        with ui.row().classes("items-center gap-4 no-wrap"):
            with ui.row().classes("items-center gap-1.5 no-wrap cursor-help") as estado_conexion:
                ui.element("div").classes("inline-block rounded-full bg-emerald-500").style(
                    "width:6px;height:6px;box-shadow:0 0 0 3px rgba(16,185,129,.15)"
                )
                ui.label("En línea").classes("text-[11px] text-slate-600 font-medium")
            estado_conexion.tooltip(
                "Conexión con el servicio local de Oficialía Digital en este equipo"
            )

            ui.element("div").style("width:1px;height:22px;background:#e2e8f0")

            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon("badge", size="18px").classes("text-slate-400")
                with ui.column().classes("gap-0 leading-tight"):
                    ui.label("Revisor en turno").classes(
                        "text-[9.5px] font-semibold uppercase tracking-wider text-slate-400"
                    )
                    ui.input(placeholder="Escriba su nombre").classes("w-44 -mt-1").props(
                        "dense outlined color=primary"
                    ).bind_value_from(revisor_ref, "valor").bind_value_to(
                        revisor_ref, "valor"
                    ).tooltip(
                        "Identifica quién confirma, descarta o reintenta oficios en el historial de auditoría"
                    )


# ----------------------------------------------------------------------
# Panel de KPIs — a la vez el único selector de filtro de la bandeja
# ----------------------------------------------------------------------
#: (clave de grupo/filtro, título, color Quasar del tono, acento hex, ícono,
#: clave del dict de `repo.contadores_kpi()`). "todos" no tiene contraparte
#: en contadores_kpi (que no agrega un total con esa clave), de ahí el
#: mapeo a "total" en la última posición.
_DEFINICIONES_KPI: list[tuple[str, str, str, str, str, str]] = [
    ("todos", "Todos", "grey-6", "#64748b", "inbox", "total"),
    ("pendientes", "Pendientes", "warning", "#d97706", "hourglass_top", "pendientes"),
    ("en_proceso", "En proceso", "info", "#0284c7", "autorenew", "en_proceso"),
    ("errores", "Errores RPA", "negative", "#e11d48", "error_outline", "errores"),
    ("completados", "Completados", "positive", "#059669", "task_alt", "completados"),
]

#: Clases Tailwind constantes de la tarjeta KPI/filtro (lo único que cambia
#: entre activa/inactiva es el borde y el fondo, aplicados vía .style()).
_KPI_CLASES_BASE = "shadow-none rounded-xl q-pa-sm flex-1 min-w-0 cursor-pointer select-none"
_KPI_CLASES_INACTIVA = f"{_KPI_CLASES_BASE} border border-slate-200 hover:border-slate-300"
_KPI_CLASES_ACTIVA = f"{_KPI_CLASES_BASE} border-2"


def panel_kpis_filtro(grupo_activo: str, on_click):
    """
    Tarjetas KPI clicables que son a la vez el ÚNICO selector de estado de la
    bandeja: sustituyen la fila separada de pestañas (que antes duplicaba
    exactamente los mismos cuatro estados) por un solo control con estado
    activo visible, además de un ícono por tarjeta para no depender solo del
    color (daltonismo).

    Devuelve (valores, repintar):
      - `valores[clave]` es el ui.label del contador, ya con las claves de
        `contadores_kpi()` ("total", "pendientes", "en_proceso", "errores",
        "completados") para refresco en vivo sin traducción adicional.
      - `repintar(grupo_activo)` reaplica el estilo de tarjeta
        seleccionada/no-seleccionada; la vista la invoca al cambiar de
        filtro, igual que antes hacía con los chips.
    """
    valores: dict[str, ui.label] = {}
    tarjetas: dict[str, ui.card] = {}

    def repintar(grupo_activo: str) -> None:
        for clave, _titulo, color_quasar, color_hex, _icono, _ck in _DEFINICIONES_KPI:
            activa = clave == grupo_activo
            tarjeta = tarjetas[clave]
            if activa:
                fondo, _texto = tono_badge(color_quasar)
                tarjeta.classes(replace=_KPI_CLASES_ACTIVA)
                tarjeta.style(
                    replace=f"border-left:3px solid {color_hex};border-color:{color_hex};"
                    f"background:{fondo};box-shadow:0 1px 2px rgba(15,23,42,.06)"
                )
            else:
                tarjeta.classes(replace=_KPI_CLASES_INACTIVA)
                tarjeta.style(replace=f"border-left:3px solid {color_hex};background:#ffffff")
            tarjeta.props(f'aria-selected={"true" if activa else "false"}')

    with ui.row().classes("w-full gap-2 no-wrap flex-wrap") as fila:
        fila.props('role=tablist aria-label="Filtrar bandeja por estado"')
        for clave, titulo, color_quasar, color_hex, icono, clave_kpi in _DEFINICIONES_KPI:
            with ui.card() as tarjeta:
                tarjeta.props(f'role=tab tabindex=0 aria-label="Filtrar por {titulo}"')
                tarjeta.on("click", lambda _=None, c=clave: on_click(c))
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.icon(icono, size="16px").style(f"color:{color_hex}")
                    ui.label(titulo).classes(
                        "text-[10.5px] font-semibold uppercase tracking-wider text-slate-500"
                    )
                valores[clave_kpi] = ui.label("0").classes(
                    "text-[22px] font-bold text-slate-800 leading-tight"
                )
            tarjetas[clave] = tarjeta

    repintar(grupo_activo)
    return valores, repintar


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


def tiempo_absoluto(iso: str) -> str:
    """'14/10/2024 08:32 hrs' — fecha/hora exacta para tooltips y auditoría.

    Un entorno hospitalario con validez legal no puede depender solo de
    tiempos relativos ("hace 13 h"); esta cadena acompaña a
    `tiempo_relativo()` como atributo `title`/tooltip en la tabla y en las
    vistas de detalle.
    """
    from datetime import datetime

    try:
        momento = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return "Fecha no disponible"
    return momento.astimezone().strftime("%d/%m/%Y %H:%M hrs")


def tarjeta_vacia(mensaje: str) -> None:
    with ui.column().classes("w-full items-center py-10 gap-1"):
        ui.icon("inbox", size="28px").classes("text-slate-300")
        ui.label(mensaje).classes("text-xs text-slate-400")
