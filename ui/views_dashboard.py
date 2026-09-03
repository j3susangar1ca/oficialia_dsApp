"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
ui/views_dashboard.py — Bandeja de entrada (página principal).

Reproduce la bandeja del frontend Svelte original:
    - Filtros de estado: Todos / Pendientes / En proceso / Errores RPA /
      Completados (chips estilo pestaña).
    - Contadores KPI siempre visibles.
    - Buscador en vivo (archivo, folio, remitente, asunto).
    - Tabla de documentos con badge de estado y tiempo relativo; clic en
      una fila → vista de revisión HITL split-screen.
    - Dropzone de carga manual (canal WEB_DRAG_DROP) con validación de
      tamaño/extensión; el pipeline de fondo hace el resto.
    - Refresco en vivo por `ui.timer` (sustituye el WebSocket original:
      SQLite local + polling de 2 s es suficiente para LAN departamental).
"""

from __future__ import annotations

import logging

from nicegui import ui

from core.models import GRUPOS_BANDEJA, OrigenIngesta, meta_estado
from core.pipeline import DocumentoDuplicado
from ui.layout import (
    encabezado,
    obtener_config,
    obtener_pipeline,
    panel_kpis,
    aplicar_tema,
    tiempo_relativo,
)

logger = logging.getLogger("oficialia.ui.dashboard")

#: Refresco de la bandeja (ms) — reemplaza los eventos WebSocket.
INTERVALO_REFRESCO_S = 2.0

#: Columnas de la tabla (etiquetas del original).
COLUMNAS = [
    {"name": "archivo", "label": "Archivo original", "field": "archivo", "align": "left", "sortable": True},
    {"name": "oficio", "label": "Oficio", "field": "oficio", "align": "left", "sortable": True},
    {"name": "estado", "label": "Estado", "field": "estado", "align": "left"},
    {"name": "paginas", "label": "Págs.", "field": "paginas", "align": "right", "sortable": True},
    {"name": "ingreso", "label": "Ingreso", "field": "ingreso", "align": "left", "sortable": True},
]

#: Mapa estado → color de badge de Quasar (paleta del estadoMeta original).
COLORES_BADGE = {
    "INGESTADO": "grey-3",
    "EN_PREPROCESO": "info",
    "EXTRAYENDO": "info",
    "PENDIENTE_REVISION": "warning",
    "EJECUTANDO_RPA": "primary",
    "ERROR_RPA": "negative",
    "COMPLETADO": "positive",
    "DESCARTADO": "grey-6",
}


def _fila_de_tabla(documento) -> dict:
    """Convierte un DocumentoRegistro en fila para ui.table."""
    info = meta_estado(documento.estado)
    return {
        "id": documento.id,
        "archivo": documento.nombre_archivo_original,
        "oficio": documento.numero_oficio or "—",
        "estado": info.etiqueta,
        "estado_color": COLORES_BADGE.get(documento.estado.value, "grey-6"),
        "paginas": documento.preproceso.num_paginas if documento.preproceso else None,
        "ingreso": tiempo_relativo(documento.fecha_ingesta),
        "_orden": documento.fecha_ingesta,
    }


@ui.page("/")
def pagina_bandeja() -> None:
    """Bandeja de entrada + carga manual."""
    aplicar_tema()
    capturista: dict = {"valor": "CAPTURISTA-DEV"}
    estado_ui: dict = {"grupo": "pendientes", "busqueda": ""}
    # Evita enviar actualizaciones WebSocket de la tabla/KPIs cuando SQLite
    # no ha cambiado; el timer sigue siendo barato y no recarga la página.
    refresco_visto: dict = {"filas": None, "kpis": None}

    # El encabezado es un layout de primer nivel (fuera del contenedor).
    encabezado(capturista)

    with ui.column().classes("w-full max-w-6xl mx-auto q-pa-md gap-4 no-wrap"):

        # ---------------- KPIs ----------------
        kpis = panel_kpis()

        # ---------------- Filtros + buscador ----------------
        chips: list[tuple[ui.button, object]] = []

        def _repintar_chips() -> None:
            """Recolorea los chips según el grupo activo."""
            for chip, grupo in chips:
                if grupo.id == estado_ui["grupo"]:
                    chip.classes("bg-primary text-white", remove="bg-slate-100 text-slate-500 hover:bg-slate-200")
                else:
                    chip.classes("bg-slate-100 text-slate-500 hover:bg-slate-200", remove="bg-primary text-white")

        with ui.row().classes("w-full items-center justify-between gap-3 no-wrap flex-wrap"):
            with ui.row().classes("gap-1 flex-wrap"):
                for grupo in GRUPOS_BANDEJA:
                    chip = ui.button(grupo.etiqueta).classes(
                        "rounded-full px-3 py-1 text-xs font-medium no-wrap shadow-none "
                        "bg-slate-100 text-slate-500 hover:bg-slate-200"
                    ).props("no-caps dense padding=2px 10px")
                    chip.on(
                        "click",
                        lambda _=None, g=grupo: (
                            estado_ui.update(grupo=g.id),
                            _repintar_chips(),
                            _refrescar(),
                        ),
                    )
                    chips.append((chip, grupo))

            ui.input(placeholder="Buscar por archivo, folio, remitente o asunto…").classes(
                "w-72"
            ).props("dense outlined color=primary clearable").bind_value_to(
                estado_ui, "busqueda"
            ).on("update:model-value", lambda _=None: _refrescar(), throttle=300)

        # ---------------- Tabla de documentos ----------------
        tabla = ui.table(columns=COLUMNAS, rows=[], row_key="id").classes("w-full shadow-1").props(
            "flat bordered dense no-data='Sin documentos en esta vista' "
            "rows-per-page-options='[25, 50, 100]' pagination=25 binary-state-sort"
        )
        tabla.add_slot(
            "body-cell-estado",
            '<q-badge :color="props.row.estado_color" :label="props.row.estado" class="q-px-sm"/>',
        )
        tabla.add_slot(
            "body-cell-archivo",
            '<div class="text-xs text-slate-700 ellipsis" style="max-width:340px" :title="props.row.archivo">'
            "{{ props.row.archivo }}</div>",
        )
        tabla.on(
            "rowClick",
            lambda e: ui.navigate.to(f"/revision/{e.args['row']['id']}"),
        )

        # ---------------- Dropzone de carga manual ----------------
        with ui.card().classes("w-full bg-slate-50 shadow-none rounded-lg"):
            with ui.row().classes("items-center justify-between no-wrap gap-4 w-full"):
                with ui.column().classes("gap-0"):
                    ui.label("Carga manual de oficios (PDF)").classes("text-sm font-semibold text-slate-700")
                    ui.label("Arrastre o seleccione archivos; el pipeline los procesará automáticamente.").classes(
                        "text-xs text-slate-400"
                    )
                ui.upload(
                    label="Subir PDFs",
                    auto_upload=True,
                    multiple=True,
                    on_upload=lambda evento: _manejar_carga(evento),
                ).props('color=primary flat accept=".pdf,application/pdf"')

    # ------------------------------------------------------------------
    # Refresco en vivo
    # ------------------------------------------------------------------
    def _grupo_actual():
        return next((g for g in GRUPOS_BANDEJA if g.id == estado_ui["grupo"]), GRUPOS_BANDEJA[0])

    def _refrescar() -> None:
        try:
            pipeline = obtener_pipeline()
            grupo = _grupo_actual()
            documentos = pipeline.repo.listar(
                estados=list(grupo.estados) if grupo.estados else None,
                texto_busqueda=estado_ui["busqueda"],
            )
            filas = [_fila_de_tabla(doc) for doc in documentos]
            firma_filas = tuple(
                (fila["id"], fila["estado"], fila["oficio"], fila["paginas"], fila["_orden"])
                for fila in filas
            )
            if firma_filas != refresco_visto["filas"]:
                tabla.rows = filas
                tabla.update()
                refresco_visto["filas"] = firma_filas

            valores_kpi = obtener_pipeline().repo.contadores_kpi()
            if valores_kpi != refresco_visto["kpis"]:
                for clave, etiqueta in kpis.items():
                    etiqueta.set_text(str(valores_kpi.get(clave, 0)))
                refresco_visto["kpis"] = valores_kpi
        except Exception:  # noqa: BLE001 — la UI nunca debe romperse por un refresh
            logger.exception("Error refrescando la bandeja")

    def _manejar_carga(evento) -> None:
        """Canal WEB_DRAG_DROP: valida y encola la ingesta en segundo plano."""
        pipeline = obtener_pipeline()
        config = obtener_config()

        nombre = evento.name or "documento.pdf"
        contenido = evento.content.read()

        if not nombre.lower().endswith(".pdf"):
            ui.notify(f"'{nombre}' no es un PDF; se ignora.", type="warning", position="top")
            return
        if len(contenido) > config.max_upload_bytes:
            ui.notify(
                f"'{nombre}' excede el límite de {config.max_upload_bytes // (1024 * 1024)} MB.",
                type="negative",
                position="top",
            )
            return
        if not contenido:
            ui.notify(f"'{nombre}' llegó vacío; se ignora.", type="warning", position="top")
            return

        try:
            pipeline.programar_ingesta(nombre, OrigenIngesta.WEB_DRAG_DROP, contenido)
            ui.notify(
                f"'{nombre}' recibido: preprocesando y extrayendo metadatos…",
                type="positive",
                position="top",
            )
            estado_ui["grupo"] = "en_proceso"
            _refrescar()
        except DocumentoDuplicado as exc:  # teórico: se lanza en el hilo de fondo
            ui.notify(f"Documento duplicado: {exc}", type="warning", position="top")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al encolar la carga de %s", nombre)
            ui.notify(f"No se pudo recibir '{nombre}': {exc}", type="negative", position="top")

    _repintar_chips()
    ui.timer(INTERVALO_REFRESCO_S, _refrescar)
    _refrescar()
