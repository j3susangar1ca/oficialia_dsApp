"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
ui/views_dashboard.py — Bandeja de entrada (página principal).

Reproduce la bandeja del frontend Svelte original y la extiende con
operativa de alto volumen. Auditoría de UI/UX aplicada en esta revisión
(ver historial de commits para el detalle completo):

    - Las tarjetas KPI (Todos / Pendientes / En proceso / Errores RPA /
      Completados) son AHORA el único selector de estado de la bandeja:
      antes existía una fila de pestañas por debajo duplicando exactamente
      los mismos cuatro estados — un solo control con estado activo visible
      reduce el desorden y la carga cognitiva (ver ui.layout.panel_kpis_filtro).
    - Barra de herramientas única: buscador + rango de fechas + indicador
      "Actualizado hace…" + exportar CSV + CTA primario "Subir PDFs", todos
      en una sola fila (antes el rango de fechas flotaba en una tercera
      línea desalineada y la carga manual quedaba al final de la pantalla,
      fuera del viewport en bandejas largas).
    - Carga manual (canal WEB_DRAG_DROP) movida a un diálogo modal disparado
      desde el CTA de la barra de herramientas, con microcopia de límites
      (tamaño máximo, formato) — en vez de una franja al pie de la página.
    - Columna "Estado" compuesta: badge de estado + calificador secundario
      ("Revisión manual campo por campo") solo cuando la extracción fue por
      respaldo heurístico, en vez de una columna "Extracción" aparte casi
      vacía con una etiqueta ambigua muy similar a la de estado.
    - Columna "Remitente" visible en la tabla: el buscador promete poder
      buscar por remitente/asunto, así que el resultado debe poder
      verificarse a simple vista sin abrir cada oficio.
    - Estados vacíos explícitos ("—") en Oficio/Remitente/Págs. en vez de
      celdas en blanco.
    - Tiempo relativo ("hace 13 h") + tooltip con fecha/hora absoluta
      (ver ui.layout.tiempo_absoluto) — trazabilidad exigible en un entorno
      con validez legal/administrativa.
    - Región `aria-live="polite"` que anuncia el tamaño de la vista actual
      para lectores de pantalla al terminar cada refresco.
    - Selección múltiple + **[Confirmar seleccionados]**: aprueba en lote
      documentos PENDIENTE_REVISION tal cual los extrajo la IA — excluye
      automáticamente los de extracción heurística (HEURISTICA_FALLBACK),
      que exigen edición manual campo por campo (ver core.pipeline.
      FlujoDocumental.confirmar_lote).
    - Refresco en vivo por `ui.timer` (sustituye el WebSocket original:
      SQLite local + polling de 2 s es suficiente para LAN departamental).
"""

from __future__ import annotations

import csv
import io
import logging
import time
from datetime import datetime

from nicegui import run, ui

from core.models import GRUPOS_BANDEJA, MetodoExtraccion, OrigenIngesta, meta_estado
from ui.layout import (
    REVISOR_POR_DEFECTO,
    aplicar_tema,
    encabezado,
    estilo_badge,
    obtener_config,
    obtener_pipeline,
    panel_kpis_filtro,
    tiempo_absoluto,
    tiempo_relativo,
)

logger = logging.getLogger("oficialia.ui.dashboard")

#: Refresco de la bandeja (ms) — reemplaza los eventos WebSocket.
INTERVALO_REFRESCO_S = 2.0

# CSS global de la bandeja (una sola vez, al importar el módulo):
#   - Filas de la tabla con affordance de "clicable" (cursor + hover), ya
#     que abren la revisión HITL pero no tenían ninguna señal visual de serlo.
#   - Se oculta el subtítulo de tamaño/porcentaje ("0.0B / 0.00%") que Quasar
#     dibuja por defecto en la cabecera del uploader incluso sin archivos en
#     cola: mezclaba el CTA de carga con un indicador de transferencia vacío.
# OJO: `shared=True` es obligatorio aquí — este módulo se importa una sola
# vez al arrancar (main.py, antes de que exista ningún cliente conectado),
# así que sin `shared=True` add_head_html intenta escribir sobre
# `context.client` (no hay ninguno todavía) y el <style> nunca llega al
# <head> real de ninguna página (verificado en navegador: el bug era
# silencioso, sin excepción, el uploader seguía mostrando "0.0B / 0.00%").
ui.add_head_html(
    """
    <style>
      .tabla-bandeja tbody tr { cursor: pointer; transition: background-color .12s ease-out; }
      .tabla-bandeja tbody tr:hover { background-color: #f8fafc; }
      .upload-limpio .q-uploader__subtitle { display: none; }
    </style>
    """,
    shared=True,
)

#: Columnas de la tabla. "Estado" incluye el calificador de método de
#: extracción (ver `_fila_de_tabla`); ya no hay una columna "Extracción"
#: aparte, que dejaba un hueco vacío en la mayoría de las filas.
COLUMNAS = [
    {"name": "archivo", "label": "Archivo original", "field": "archivo", "align": "left", "sortable": True},
    {"name": "oficio", "label": "Oficio", "field": "oficio", "align": "left", "sortable": True},
    {"name": "remitente", "label": "Remitente", "field": "remitente", "align": "left", "sortable": True},
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

#: Etiqueta legible del método de extracción, solo para la exportación CSV
#: (en la tabla se resume como calificador del badge de estado, ver abajo).
_METODO_ETIQUETA_CSV = {
    MetodoExtraccion.IA.value: "Automático (IA)",
    MetodoExtraccion.HEURISTICA_FALLBACK.value: "Heurística — revisión manual",
    MetodoExtraccion.HITL.value: "Manual",
}


def _fila_de_tabla(documento) -> dict:
    """Convierte un DocumentoRegistro en fila para ui.table."""
    info = meta_estado(documento.estado)
    fuente = documento.metadatos_extraidos or documento.metadatos_validados
    es_heuristico = documento.extraccion_metodo == MetodoExtraccion.HEURISTICA_FALLBACK
    return {
        "id": documento.id,
        "archivo": documento.nombre_archivo_original,
        "oficio": documento.numero_oficio or "—",
        "remitente": (fuente.remitente_nombre if fuente else "") or "—",
        "estado": info.etiqueta,
        "estado_style": estilo_badge(COLORES_BADGE.get(documento.estado.value, "grey-6")),
        # Calificador secundario del badge de estado (no un segundo badge
        # aparte): evita la ambigüedad de dos etiquetas de color casi
        # idéntico ("Por revisar" / "Requiere revisión") sin relación clara.
        "estado_calificador": "Revisión manual campo por campo" if es_heuristico else "",
        "paginas": documento.preproceso.num_paginas if documento.preproceso else None,
        "ingreso": tiempo_relativo(documento.fecha_ingesta),
        "ingreso_abs": tiempo_absoluto(documento.fecha_ingesta),
        "_orden": documento.fecha_ingesta,
    }


@ui.page("/")
def pagina_bandeja() -> None:
    """Bandeja de entrada + carga manual."""
    aplicar_tema()
    revisor: dict = {"valor": ""}
    config = obtener_config()
    # fecha_desde/fecha_hasta ya deben existir aquí: _calcular_filas() (más
    # abajo) los lee antes de que los ui.input del rango de fechas alcancen
    # a poblarlos vía bind_value_to, y un KeyError en esa primera pasada
    # dejaba la tabla en blanco hasta el primer _refrescar() (detectado al
    # verificar esta pantalla en navegador).
    estado_ui: dict = {
        "grupo": "pendientes", "busqueda": "", "fecha_desde": "", "fecha_hasta": "",
        "ultima_actualizacion": None,
        # Preexistente a esta revisión: si "seleccion" no existe todavía en
        # el diccionario, bind_visibility_from no logra leerla en su primer
        # ciclo y la barra de acciones en lote queda VISIBLE por defecto sin
        # nada seleccionado (verificado en navegador). Se inicializa vacía
        # para que el binding siempre tenga un valor válido que leer.
        "seleccion": [],
    }
    # Evita enviar actualizaciones WebSocket de la tabla/KPIs cuando SQLite
    # no ha cambiado; el timer sigue siendo barato y no recarga la página.
    refresco_visto: dict = {"filas": None, "kpis": None}

    # El encabezado es un layout de primer nivel (fuera del contenedor).
    encabezado(revisor)

    def _grupo_actual():
        return next((g for g in GRUPOS_BANDEJA if g.id == estado_ui["grupo"]), GRUPOS_BANDEJA[0])

    def _calcular_filas() -> list[dict]:
        grupo = _grupo_actual()
        documentos = obtener_pipeline().repo.listar(
            estados=list(grupo.estados) if grupo.estados else None,
            texto_busqueda=estado_ui["busqueda"],
            fecha_desde=estado_ui.get("fecha_desde") or None,
            fecha_hasta=estado_ui.get("fecha_hasta") or None,
        )
        return [_fila_de_tabla(doc) for doc in documentos]

    # Filas iniciales calculadas ANTES de construir la tabla: con
    # selection="multiple", crear el ui.table con rows=[] y poblarlo recién
    # después (tabla.rows = […]; tabla.update()) deja el checkbox "seleccionar
    # todo" de Quasar en un estado intermedio inconsistente (arranca vacío,
    # luego se llena) que dispara un TypeError interno de Quasar en el
    # navegador (verificado: no rompe la funcionalidad, pero es evitable).
    # Pasar las filas reales desde el constructor evita esa transición.
    try:
        filas_iniciales = _calcular_filas()
    except Exception:  # noqa: BLE001 — igual que _refrescar(): la UI nunca debe romperse
        logger.exception("Error calculando las filas iniciales de la bandeja")
        filas_iniciales = []

    with ui.column().classes("w-full max-w-6xl mx-auto q-pa-md gap-4 no-wrap"):

        # ---------------- KPIs = único selector de filtro ----------------
        def _cambiar_grupo(nuevo_grupo: str) -> None:
            estado_ui.update(grupo=nuevo_grupo)
            repintar_kpis(estado_ui["grupo"])
            _limpiar_seleccion()
            _refrescar()

        kpis_valores, repintar_kpis = panel_kpis_filtro(estado_ui["grupo"], _cambiar_grupo)

        # ---------------- Barra de herramientas unificada ----------------
        # Buscador + rango de fechas + estado de sincronización + exportar +
        # CTA de carga, todos en una sola fila (antes dispersos en 3 líneas
        # distintas: pestañas+buscador, fechas en línea aparte, carga al pie
        # de la página).
        with ui.row().classes("w-full items-center justify-between gap-3 no-wrap flex-wrap"):
            with ui.row().classes("items-center gap-2 flex-wrap"):
                # OJO: se escribe en estado_ui DIRECTO desde el evento
                # (e.value), no solo vía bind_value_to — bind_value_to
                # propaga por el bucle de refresco periódico de NiceGUI (no
                # sincrónico), así que _refrescar() podía correr con el
                # valor previo todavía en el diccionario si dependía solo
                # del binding. bind_value_to se deja igual para el resto de
                # la UI reactiva (ej. visibilidad de "Limpiar rango"), donde
                # la eventual consistencia no importa.
                # w-[380px] fijo en vez de flex-grow: la fila contenedora no
                # es w-full, así que flex-grow no tenía espacio disponible
                # para crecer y el placeholder completo quedaba recortado
                # visualmente (verificado en navegador).
                ui.input(placeholder="Buscar por archivo, folio, remitente o asunto…").classes(
                    "w-[380px]"
                ).props('dense outlined color=primary clearable debounce="300"').bind_value_to(
                    estado_ui, "busqueda"
                ).on_value_change(lambda e: (estado_ui.update(busqueda=e.value or ""), _refrescar()))

                ui.label("Ingresados entre").classes("text-xs text-slate-500 no-wrap")
                ui.input().props("dense outlined color=primary type=date").classes("w-36").bind_value_to(
                    estado_ui, "fecha_desde"
                ).on_value_change(lambda e: (estado_ui.update(fecha_desde=e.value or ""), _refrescar()))
                ui.label("y").classes("text-xs text-slate-500")
                ui.input().props("dense outlined color=primary type=date").classes("w-36").bind_value_to(
                    estado_ui, "fecha_hasta"
                ).on_value_change(lambda e: (estado_ui.update(fecha_hasta=e.value or ""), _refrescar()))
                ui.button("Limpiar rango", icon="close").props("flat dense no-caps color=grey").on_click(
                    lambda: (estado_ui.update(fecha_desde="", fecha_hasta=""), _refrescar())
                ).bind_visibility_from(
                    estado_ui, "fecha_desde", backward=lambda v: bool(v) or bool(estado_ui.get("fecha_hasta"))
                )

            with ui.row().classes("items-center gap-3 no-wrap"):
                with ui.row().classes("items-center gap-0.5 no-wrap"):
                    etiqueta_actualizado = ui.label("Actualizando…").classes(
                        "text-[11px] text-slate-400 no-wrap"
                    )
                    ui.button(icon="refresh").props("flat dense round color=grey size=sm").on_click(
                        lambda: _refrescar()
                    ).tooltip("Actualizar ahora")
                ui.button("Exportar CSV", icon="download").props(
                    "flat dense no-caps color=grey"
                ).on_click(lambda: _exportar_csv())
                ui.button("Subir PDFs", icon="upload_file").props(
                    "color=primary no-caps unelevated"
                ).on_click(lambda: dialogo_carga.open()).tooltip(
                    f"Máx. {config.max_upload_bytes // (1024 * 1024)} MB por archivo · solo PDF"
                )

        # ---------------- Barra de acciones en lote ----------------
        with ui.row().classes("w-full items-center gap-2 no-wrap") as barra_lote:
            ui.label().classes("text-xs text-slate-600 font-medium").bind_text_from(
                estado_ui, "seleccion", backward=lambda ids: f"{len(ids or [])} documento(s) seleccionado(s)"
            )
            ui.button("Confirmar seleccionados", icon="playlist_add_check").props(
                "color=primary no-caps dense"
            ).on_click(lambda: _confirmar_lote())
            ui.button("Quitar selección", icon="close").props("flat dense no-caps color=grey").on_click(
                lambda: _limpiar_seleccion()
            )
        barra_lote.bind_visibility_from(
            estado_ui, "seleccion", backward=lambda ids: estado_ui["grupo"] == "pendientes" and bool(ids)
        )

        # ---------------- Tabla de documentos ----------------
        # OJO: `pagination` va como kwarg del constructor (Table lo envuelve en
        # {'rowsPerPage': N}, el objeto que espera QTable), NUNCA como texto en
        # .props("pagination=25") — un valor plano ahí, combinado con
        # rows-per-page-options, deja el objeto de paginación interno de Quasar
        # inválido y la tabla renderiza 0 filas pese a tener datos (bug real,
        # verificado en navegador: nunca antes se había probado esta pantalla
        # fuera de un curl/HTTP plano).
        # `on_select` se conecta DESPUÉS de construir la tabla, no como kwarg
        # del constructor: pasarlo ahí lo registra antes de que el componente
        # QTable de Quasar termine de montarse en el cliente y dispara un
        # TypeError interno de Quasar en cada carga (verificado en navegador;
        # no rompía la selección en sí, pero es evitable).
        tabla = ui.table(
            columns=COLUMNAS, rows=filas_iniciales, row_key="id", selection="multiple", pagination=25,
        ).classes("w-full rounded-xl overflow-hidden tabla-bandeja").props(
            "flat bordered dense no-data='Sin documentos en esta vista' "
            "rows-per-page-options='[25, 50, 100]' binary-state-sort"
        )
        # Badges "soft pill" (fondo claro + texto del mismo tono) en vez del
        # q-badge sólido por defecto: mismo lenguaje visual en toda la app
        # (ver ui.layout.estilo_badge, que calcula estado_style).
        #
        # OJO — bug real encontrado al verificar esta pantalla en navegador
        # con datos (no solo con la tabla vacía): un slot "body-cell-X" cuyo
        # contenido NO está envuelto en <q-td> no genera una celda de tabla
        # real, sino un <div>/<span> suelto como hijo directo de <tr>. Como
        # Vue inserta esos nodos vía DOM API (no vía el parser HTML), el
        # navegador NO hace foster-parenting: los agrupa en UNA sola celda
        # anónima por cada RACHA de hijos sin <td> consecutivos. Con dos o
        # más columnas personalizadas seguidas (como pasaba antes con
        # "estado"+"metodo"), su contenido termina apilado en una sola
        # columna visual — probablemente la causa real de que el badge de
        # estado y el de método parecieran "dos etiquetas contiguas
        # ambiguas" en la captura original de la auditoría. Cada slot debe
        # envolver su contenido en <q-td :props="props">, como documenta
        # Quasar para body-cell-*.
        tabla.add_slot(
            "body-cell-estado",
            '<q-td key="estado" :props="props">'
            '<div class="flex flex-col gap-0.5 py-1">'
            '<span class="rounded-full px-2.5 py-0.5 text-[11px] font-medium w-fit" '
            ':style="props.row.estado_style">{{ props.row.estado }}</span>'
            '<span v-if="props.row.estado_calificador" '
            'class="text-[10px] font-medium text-orange-700 flex items-center gap-1">'
            '<span aria-hidden="true">⚠</span>{{ props.row.estado_calificador }}</span>'
            "</div></q-td>",
        )
        tabla.add_slot(
            "body-cell-archivo",
            '<q-td key="archivo" :props="props">'
            '<div class="text-xs text-slate-700 ellipsis" style="max-width:280px" :title="props.row.archivo">'
            "{{ props.row.archivo }}</div></q-td>",
        )
        tabla.add_slot(
            "body-cell-remitente",
            '<q-td key="remitente" :props="props">'
            '<div class="text-xs text-slate-600 ellipsis" style="max-width:200px" :title="props.row.remitente">'
            "{{ props.row.remitente }}</div></q-td>",
        )
        tabla.add_slot(
            "body-cell-paginas",
            '<q-td key="paginas" :props="props">'
            "<span class='text-xs text-slate-600'>"
            "{{ props.row.paginas === null || props.row.paginas === undefined ? '—' : props.row.paginas }}"
            "</span></q-td>",
        )
        tabla.add_slot(
            "body-cell-ingreso",
            '<q-td key="ingreso" :props="props">'
            '<span class="text-xs text-slate-500" :title="props.row.ingreso_abs">{{ props.row.ingreso }}</span>'
            "</q-td>",
        )
        tabla.on(
            "rowClick",
            lambda e: ui.navigate.to(
                f"/revision/{e.args[1]['id'] if isinstance(e.args, list) and len(e.args) > 1 else e.args.get('row', {}).get('id')}"
            ),
        )
        tabla.on_select(lambda e: estado_ui.update(seleccion=[fila["id"] for fila in tabla.selected]))

        # ---------------- Región viva para lectores de pantalla ----------------
        # Anuncia el tamaño de la vista actual cada vez que cambian las filas
        # (pipeline automatizado corriendo en segundo plano sin recarga de
        # página: sin esto, un lector de pantalla no se entera de que algo
        # cambió).
        region_anuncio = ui.label("").classes("sr-only").props("role=status aria-live=polite")

        # ---------------- Diálogo de carga manual ----------------
        # Disparado desde el CTA "Subir PDFs" de la barra de herramientas
        # (antes: franja al pie de la página, fuera del viewport en bandejas
        # largas). `.upload-limpio` oculta el subtítulo "0.0B / 0.00%" que
        # Quasar dibuja por defecto (ver <style> al importar el módulo).
        with ui.dialog() as dialogo_carga, ui.card().classes("w-full max-w-md gap-3 q-pa-md rounded-xl"):
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon("upload_file", size="20px").classes("text-primary")
                ui.label("Carga manual de oficios (PDF)").classes("text-sm font-semibold text-slate-700")
            ui.label(
                f"Máx. {config.max_upload_bytes // (1024 * 1024)} MB por archivo · solo PDF "
                "(escaneado o vectorial) · puede seleccionar o arrastrar varios documentos a la vez."
            ).classes("text-xs text-slate-400")
            ui.upload(
                label="Arrastre archivos aquí o haga clic para elegir",
                auto_upload=True,
                multiple=True,
                on_upload=lambda evento: _manejar_carga(evento),
            ).props('color=primary flat bordered accept=".pdf,application/pdf"').classes(
                "w-full upload-limpio"
            )
            with ui.row().classes("w-full justify-end"):
                ui.button("Cerrar", icon="close").props("flat no-caps color=grey").on_click(dialogo_carga.close)

    # ------------------------------------------------------------------
    # Refresco en vivo (_grupo_actual/_calcular_filas ya definidas arriba,
    # reutilizadas para las filas iniciales de la tabla)
    # ------------------------------------------------------------------
    def _texto_actualizado() -> str:
        ultimo = estado_ui.get("ultima_actualizacion")
        if not ultimo:
            return "Actualizando…"
        segundos = int(time.time() - ultimo)
        if segundos < 5:
            return "Actualizado justo ahora"
        if segundos < 60:
            return f"Actualizado hace {segundos} s"
        return f"Actualizado hace {segundos // 60} min"

    def _refrescar() -> None:
        try:
            pipeline = obtener_pipeline()
            grupo = _grupo_actual()
            documentos = pipeline.repo.listar(
                estados=list(grupo.estados) if grupo.estados else None,
                texto_busqueda=estado_ui["busqueda"],
                fecha_desde=estado_ui.get("fecha_desde") or None,
                fecha_hasta=estado_ui.get("fecha_hasta") or None,
            )
            filas = [_fila_de_tabla(doc) for doc in documentos]
            firma_filas = tuple(
                (fila["id"], fila["estado"], fila["estado_calificador"], fila["oficio"],
                 fila["remitente"], fila["paginas"], fila["_orden"])
                for fila in filas
            )
            if firma_filas != refresco_visto["filas"]:
                tabla.rows = filas
                tabla.update()
                refresco_visto["filas"] = firma_filas
                region_anuncio.set_text(f"{len(filas)} documento(s) en la vista {grupo.etiqueta}.")

            valores_kpi = obtener_pipeline().repo.contadores_kpi()
            if valores_kpi != refresco_visto["kpis"]:
                for clave, etiqueta in kpis_valores.items():
                    etiqueta.set_text(str(valores_kpi.get(clave, 0)))
                refresco_visto["kpis"] = valores_kpi

            estado_ui["ultima_actualizacion"] = time.time()
            etiqueta_actualizado.set_text(_texto_actualizado())
        except Exception:  # noqa: BLE001 — la UI nunca debe romperse por un refresh
            logger.exception("Error refrescando la bandeja")

    def _limpiar_seleccion() -> None:
        tabla.selected = []
        tabla.update()
        estado_ui["seleccion"] = []

    async def _confirmar_lote() -> None:
        # .get(..., []): "seleccion" solo existe en estado_ui desde el primer
        # tabla.on_select/_limpiar_seleccion — indexar directo lanzaba
        # KeyError si el botón se alcanzaba a click-ear antes de esa primera
        # escritura (visto en producción, ver barra_lote más abajo).
        ids = list(estado_ui.get("seleccion") or [])
        if not ids:
            return
        pipeline = obtener_pipeline()
        nombre_revisor = revisor["valor"].strip() or REVISOR_POR_DEFECTO
        try:
            resultado = await run.io_bound(pipeline.confirmar_lote, ids, nombre_revisor)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo la confirmación en lote")
            ui.notify(f"No se pudo confirmar el lote: {exc}", type="negative", position="top")
            return

        if resultado.confirmados:
            ui.notify(
                f"{len(resultado.confirmados)} documento(s) confirmado(s) y en registro RPA.",
                type="positive", position="top",
            )
        if resultado.omitidos:
            detalle = "; ".join(f"{doc_id[:8]}…: {motivo}" for doc_id, motivo in resultado.omitidos[:5])
            if len(resultado.omitidos) > 5:
                detalle += f" (+{len(resultado.omitidos) - 5} más)"
            ui.notify(
                f"{len(resultado.omitidos)} documento(s) omitido(s): {detalle}",
                type="warning", position="top", multi_line=True, timeout=8000,
            )
        _limpiar_seleccion()
        _refrescar()

    def _manejar_carga(evento) -> None:
        """Canal WEB_DRAG_DROP: valida y encola la ingesta en segundo plano."""
        pipeline = obtener_pipeline()

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

        # OJO: verificar_duplicado() se llama ANTES de encolar, NO dentro
        # de un try/except alrededor de programar_ingesta(). programar_
        # ingesta() es fire-and-forget (ThreadPoolExecutor.submit sin
        # .result()): si DocumentoDuplicado se lanza allá dentro, en el
        # hilo de fondo, esa excepción no tiene forma de llegar hasta acá
        # — un except aquí alrededor de programar_ingesta() nunca se
        # dispara (verificado en navegador: subir el mismo archivo dos
        # veces mostraba igual el toast de éxito). Este chequeo síncrono
        # y barato (hash + lectura SQLite) es lo que sí puede avisar en la
        # UI; el chequeo de ingestar_y_procesar() sigue como red de
        # seguridad para la carrera minúscula entre ambos (ver
        # FlujoDocumental.verificar_duplicado).
        try:
            duplicado = pipeline.verificar_duplicado(contenido)
        except Exception:  # noqa: BLE001 — el chequeo nunca debe bloquear la carga
            logger.exception("Fallo verificando duplicado de %s; se continúa con la carga", nombre)
            duplicado = None
        if duplicado is not None:
            _mostrar_dialogo_duplicado(nombre, duplicado)
            return

        try:
            pipeline.programar_ingesta(nombre, OrigenIngesta.WEB_DRAG_DROP, contenido)
            ui.notify(
                f"'{nombre}' recibido: preprocesando y extrayendo metadatos…",
                type="positive",
                position="top",
            )
            _cambiar_grupo("en_proceso")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al encolar la carga de %s", nombre)
            ui.notify(f"No se pudo recibir '{nombre}': {exc}", type="negative", position="top")

    def _mostrar_dialogo_duplicado(nombre_subido: str, existente) -> None:
        """Diálogo (no un ui.notify de solo texto): ui.notify serializa un
        payload JSON sin bridging a Python, así que no puede llevar un botón
        con acción real — un diálogo sí, y es la única forma de ofrecer
        aquí una acción de un clic en vez de una notificación pasiva.

        "Sobrescribir"/"Crear nueva versión" quedan fuera a propósito: el
        esquema no tiene noción de versión de documento y sobrescribir un
        registro con posible validez legal/RPA ya ejecutado es una
        decisión de producto, no de UI — la acción real disponible hoy es
        ver el documento existente.
        """
        info = meta_estado(existente.estado)
        with ui.dialog() as dialogo, ui.card().classes("gap-3 q-pa-md max-w-sm rounded-xl"):
            with ui.row().classes("items-center gap-2 no-wrap"):
                ui.icon("content_copy", size="20px").classes("text-amber-600")
                ui.label("Documento ya registrado").classes("text-sm font-semibold text-slate-700")
            ui.label(
                f"«{nombre_subido}» tiene el mismo contenido que un documento que ya está en el "
                "sistema; no se creó un registro nuevo."
            ).classes("text-xs text-slate-500")
            with ui.row().classes("items-center gap-2 no-wrap bg-slate-50 rounded-lg q-pa-sm"):
                ui.icon("description", size="16px").classes("text-slate-400")
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(existente.nombre_archivo_original).classes(
                        "text-xs font-medium text-slate-700 ellipsis"
                    ).style("max-width:260px")
                    ui.label(info.etiqueta).classes(
                        "rounded-full px-2 py-0.5 text-[10px] font-medium w-fit"
                    ).style(estilo_badge(info.color))
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cerrar").props("flat no-caps color=grey").on_click(dialogo.close)
                ui.button("Ver documento existente", icon="open_in_new").props(
                    "color=primary no-caps"
                ).on_click(lambda: (dialogo.close(), ui.navigate.to(f"/revision/{existente.id}")))
        dialogo.open()

    def _exportar_csv() -> None:
        """Exporta a CSV la vista actual (mismo filtro de estado/búsqueda/fechas
        aplicado en pantalla) — bitácoras de turno y entregas de guardia."""
        grupo = _grupo_actual()
        try:
            documentos = obtener_pipeline().repo.listar(
                estados=list(grupo.estados) if grupo.estados else None,
                texto_busqueda=estado_ui["busqueda"],
                fecha_desde=estado_ui.get("fecha_desde") or None,
                fecha_hasta=estado_ui.get("fecha_hasta") or None,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Error preparando la exportación CSV")
            ui.notify("No se pudo preparar la exportación.", type="negative", position="top")
            return

        buffer = io.StringIO()
        escritor = csv.writer(buffer)
        escritor.writerow(
            ["Archivo", "Oficio", "Remitente", "Estado", "Método de extracción", "Páginas", "Ingreso"]
        )
        for documento in documentos:
            fila = _fila_de_tabla(documento)
            escritor.writerow([
                fila["archivo"],
                fila["oficio"],
                fila["remitente"],
                fila["estado"],
                _METODO_ETIQUETA_CSV.get(documento.extraccion_metodo.value, documento.extraccion_metodo.value),
                fila["paginas"] if fila["paginas"] is not None else "",
                fila["ingreso_abs"],
            ])

        nombre_archivo = f"oficialia_{grupo.id}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
        # BOM (utf-8-sig) para que Excel detecte UTF-8 y no vuelva a romper
        # los acentos — precisamente el bug de codificación que motivó esta
        # auditoría.
        ui.download(buffer.getvalue().encode("utf-8-sig"), nombre_archivo)
        ui.notify(f"Exportando {len(documentos)} documento(s) a CSV…", type="positive", position="top")

    ui.timer(INTERVALO_REFRESCO_S, _refrescar)
    ui.timer(1.0, lambda: etiqueta_actualizado.set_text(_texto_actualizado()))
    _refrescar()
