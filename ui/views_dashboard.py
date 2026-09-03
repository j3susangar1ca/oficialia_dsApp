"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
ui/views_dashboard.py — Bandeja de entrada (página principal).

Reproduce la bandeja del frontend Svelte original y la extiende con
operativa de alto volumen:
    - Filtros de estado: Todos / Pendientes / En proceso / Errores RPA /
      Completados (chips estilo pestaña).
    - Contadores KPI siempre visibles.
    - Buscador en vivo (archivo, folio, remitente, asunto) + filtro de
      rango de fechas de ingesta (útil sobre el histórico ya archivado).
    - Tabla de documentos con badge de estado/método de extracción y
      tiempo relativo; clic en una fila → vista de revisión HITL split-screen.
    - Selección múltiple + **[Confirmar seleccionados]**: aprueba en lote
      documentos PENDIENTE_REVISION tal cual los extrajo la IA — excluye
      automáticamente los de extracción heurística (HEURISTICA_FALLBACK),
      que exigen edición manual campo por campo (ver core.pipeline.
      FlujoDocumental.confirmar_lote).
    - Dropzone de carga manual (canal WEB_DRAG_DROP) con validación de
      tamaño/extensión; el pipeline de fondo hace el resto.
    - Refresco en vivo por `ui.timer` (sustituye el WebSocket original:
      SQLite local + polling de 2 s es suficiente para LAN departamental).
"""

from __future__ import annotations

import logging

from nicegui import run, ui

from core.models import GRUPOS_BANDEJA, MetodoExtraccion, OrigenIngesta, meta_estado
from core.pipeline import DocumentoDuplicado
from ui.layout import (
    REVISOR_POR_DEFECTO,
    encabezado,
    estilo_badge,
    obtener_config,
    obtener_pipeline,
    panel_kpis,
    aplicar_tema,
    tiempo_relativo,
)

logger = logging.getLogger("oficialia.ui.dashboard")

#: Refresco de la bandeja (ms) — reemplaza los eventos WebSocket.
INTERVALO_REFRESCO_S = 2.0

#: Columnas de la tabla (etiquetas del original + método de extracción).
COLUMNAS = [
    {"name": "archivo", "label": "Archivo original", "field": "archivo", "align": "left", "sortable": True},
    {"name": "oficio", "label": "Oficio", "field": "oficio", "align": "left", "sortable": True},
    {"name": "estado", "label": "Estado", "field": "estado", "align": "left"},
    {"name": "metodo", "label": "Extracción", "field": "metodo", "align": "left"},
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

#: Etiqueta + color del método de extracción (columna "Extracción"). Sin
#: mencionar el proveedor de IA: solo importa si el dato quedó listo para
#: confirmar tal cual o si requiere revisión campo por campo.
METODO_ETIQUETA = {
    MetodoExtraccion.IA.value: ("Automático", "grey-7"),
    MetodoExtraccion.HEURISTICA_FALLBACK.value: ("Requiere revisión", "orange-8"),
    MetodoExtraccion.HITL.value: ("Manual", "grey-7"),
}


def _fila_de_tabla(documento) -> dict:
    """Convierte un DocumentoRegistro en fila para ui.table."""
    info = meta_estado(documento.estado)
    metodo_etiqueta, metodo_color = METODO_ETIQUETA.get(
        documento.extraccion_metodo.value, (documento.extraccion_metodo.value, "grey-7")
    )
    return {
        "id": documento.id,
        "archivo": documento.nombre_archivo_original,
        "oficio": documento.numero_oficio or "—",
        "estado": info.etiqueta,
        "estado_style": estilo_badge(COLORES_BADGE.get(documento.estado.value, "grey-6")),
        "metodo": metodo_etiqueta,
        "metodo_style": estilo_badge(metodo_color),
        "paginas": documento.preproceso.num_paginas if documento.preproceso else None,
        "ingreso": tiempo_relativo(documento.fecha_ingesta),
        "_orden": documento.fecha_ingesta,
    }


@ui.page("/")
def pagina_bandeja() -> None:
    """Bandeja de entrada + carga manual."""
    aplicar_tema()
    revisor: dict = {"valor": ""}
    # fecha_desde/fecha_hasta ya deben existir aquí: _calcular_filas() (más
    # abajo) los lee antes de que los ui.input del rango de fechas alcancen
    # a poblarlos vía bind_value_to, y un KeyError en esa primera pasada
    # dejaba la tabla en blanco hasta el primer _refrescar() (detectado al
    # verificar esta pantalla en navegador).
    estado_ui: dict = {"grupo": "pendientes", "busqueda": "", "fecha_desde": "", "fecha_hasta": ""}
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
            fecha_desde=estado_ui["fecha_desde"] or None,
            fecha_hasta=estado_ui["fecha_hasta"] or None,
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

        # ---------------- KPIs ----------------
        kpis = panel_kpis()

        # ---------------- Filtros + buscador ----------------
        chips: list[tuple[ui.button, object]] = []

        _CHIP_ACTIVO = "bg-white text-slate-800 shadow-sm"
        _CHIP_INACTIVO = "bg-transparent text-slate-500 hover:text-slate-700"

        def _repintar_chips() -> None:
            """Recolorea los chips según el grupo activo (estilo control segmentado)."""
            for chip, grupo in chips:
                if grupo.id == estado_ui["grupo"]:
                    chip.classes(_CHIP_ACTIVO, remove=_CHIP_INACTIVO)
                else:
                    chip.classes(_CHIP_INACTIVO, remove=_CHIP_ACTIVO)

        with ui.row().classes("w-full items-center justify-between gap-3 no-wrap flex-wrap"):
            with ui.row().classes("gap-1 p-1 bg-slate-100 rounded-lg flex-wrap"):
                for grupo in GRUPOS_BANDEJA:
                    chip = ui.button(grupo.etiqueta).classes(
                        "rounded-md px-3 py-1 text-xs font-medium no-wrap shadow-none "
                        "transition-colors"
                    ).props('no-caps dense flat padding="4px 10px"')
                    chip.on(
                        "click",
                        lambda _=None, g=grupo: (
                            estado_ui.update(grupo=g.id),
                            _repintar_chips(),
                            _limpiar_seleccion(),
                            _refrescar(),
                        ),
                    )
                    chips.append((chip, grupo))

            # OJO: se escribe en estado_ui DIRECTO desde el evento (e.value),
            # no solo vía bind_value_to — bind_value_to propaga por el bucle
            # de refresco periódico de NiceGUI (no sincrónico), así que
            # _refrescar() podía correr con el valor previo todavía en el
            # diccionario si dependía solo del binding. bind_value_to se deja
            # igual para el resto de la UI reactiva (ej. visibilidad de
            # "Limpiar rango"), donde la eventual consistencia no importa.
            ui.input(placeholder="Buscar por archivo, folio, remitente o asunto…").classes(
                "w-72"
            ).props('dense outlined color=primary clearable debounce="300"').bind_value_to(
                estado_ui, "busqueda"
            ).on_value_change(lambda e: (estado_ui.update(busqueda=e.value or ""), _refrescar()))

        # ---------------- Rango de fechas de ingesta ----------------
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            ui.label("Ingresados entre").classes("text-xs text-slate-500")
            ui.input().props("dense outlined color=primary type=date").classes("w-40").bind_value_to(
                estado_ui, "fecha_desde"
            ).on_value_change(lambda e: (estado_ui.update(fecha_desde=e.value or ""), _refrescar()))
            ui.label("y").classes("text-xs text-slate-500")
            ui.input().props("dense outlined color=primary type=date").classes("w-40").bind_value_to(
                estado_ui, "fecha_hasta"
            ).on_value_change(lambda e: (estado_ui.update(fecha_hasta=e.value or ""), _refrescar()))
            ui.button("Limpiar rango", icon="close").props("flat dense no-caps color=grey").on_click(
                lambda: (estado_ui.update(fecha_desde="", fecha_hasta=""), _refrescar())
            ).bind_visibility_from(
                estado_ui, "fecha_desde", backward=lambda v: bool(v) or bool(estado_ui.get("fecha_hasta"))
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
        ).classes("w-full rounded-xl overflow-hidden").props(
            "flat bordered dense no-data='Sin documentos en esta vista' "
            "rows-per-page-options='[25, 50, 100]' binary-state-sort"
        )
        # Badges "soft pill" (fondo claro + texto del mismo tono) en vez del
        # q-badge sólido por defecto: mismo lenguaje visual en toda la app
        # (ver ui.layout.estilo_badge, que calcula estado_style/metodo_style).
        # Se mantiene la misma estructura de slot (sin envolver en <q-td>,
        # ver nota de "on_select"/"pagination" arriba: esta pantalla ya se
        # verificó en navegador con slots "planos" como estos).
        tabla.add_slot(
            "body-cell-estado",
            '<span class="rounded-full px-2.5 py-0.5 text-[11px] font-medium" '
            ':style="props.row.estado_style">{{ props.row.estado }}</span>',
        )
        tabla.add_slot(
            "body-cell-metodo",
            '<span class="rounded-full px-2.5 py-0.5 text-[11px] font-medium" '
            ':style="props.row.metodo_style">{{ props.row.metodo }}</span>',
        )
        tabla.add_slot(
            "body-cell-archivo",
            '<div class="text-xs text-slate-700 ellipsis" style="max-width:340px" :title="props.row.archivo">'
            "{{ props.row.archivo }}</div>",
        )
        tabla.on(
            "rowClick",
            lambda e: ui.navigate.to(
                f"/revision/{e.args[1]['id'] if isinstance(e.args, list) and len(e.args) > 1 else e.args.get('row', {}).get('id')}"
            ),
        )
        tabla.on_select(lambda e: estado_ui.update(seleccion=[fila["id"] for fila in tabla.selected]))

        # ---------------- Dropzone de carga manual ----------------
        with ui.card().classes(
            "w-full bg-white shadow-none rounded-xl border border-dashed border-slate-300"
        ):
            with ui.row().classes("items-center justify-between no-wrap gap-4 w-full"):
                with ui.row().classes("items-center gap-3 no-wrap"):
                    ui.icon("upload_file", size="22px").classes("text-slate-400")
                    with ui.column().classes("gap-0"):
                        ui.label("Carga manual de oficios (PDF)").classes(
                            "text-sm font-semibold text-slate-700"
                        )
                        ui.label(
                            "Arrastre o seleccione archivos; el pipeline los procesará automáticamente."
                        ).classes("text-xs text-slate-400")
                ui.upload(
                    label="Subir PDFs",
                    auto_upload=True,
                    multiple=True,
                    on_upload=lambda evento: _manejar_carga(evento),
                ).props('color=primary flat accept=".pdf,application/pdf"')

    # ------------------------------------------------------------------
    # Refresco en vivo (_grupo_actual/_calcular_filas ya definidas arriba,
    # reutilizadas para las filas iniciales de la tabla)
    # ------------------------------------------------------------------
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

    def _limpiar_seleccion() -> None:
        tabla.selected = []
        tabla.update()
        estado_ui["seleccion"] = []

    async def _confirmar_lote() -> None:
        ids = list(estado_ui["seleccion"])
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
            _repintar_chips()
            _limpiar_seleccion()
            _refrescar()
        except DocumentoDuplicado as exc:  # teórico: se lanza en el hilo de fondo
            ui.notify(f"Documento duplicado: {exc}", type="warning", position="top")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al encolar la carga de %s", nombre)
            ui.notify(f"No se pudo recibir '{nombre}': {exc}", type="negative", position="top")

    _repintar_chips()
    ui.timer(INTERVALO_REFRESCO_S, _refrescar)
    _refrescar()
