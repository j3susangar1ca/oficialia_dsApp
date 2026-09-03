"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
ui/views_hitl.py — Revisión asistida Human-in-the-Loop (split-screen 50/50).

Migración del `HitlReviewView.svelte` original:

    - Panel izquierdo: visor de PDF integrado (sirve el archivo actual del
      documento vía la ruta `/pdf/{id}` registrada en main.py) con
      navegación de páginas y apertura en pestaña nueva.
    - Panel derecho: formulario reactivo precargado con la extracción de
      la IA, validación en vivo campo a campo (mismas reglas del contrato
      `MetadatosOficio`) y acciones operativas:
        [Confirmar y Registrar] → nomenclatura canónica + JSON espejo + RPA
        [Descartar]             → estado terminal, archivo aislado en 04_errores
        [Reintentar RPA]        → reinyección en ERROR_RPA (sin reextraer)
    - Banners de contexto: ERROR_RPA (con motivo y reintento), COMPLETADO
      (folio de acuse + evidencia) y DESCARTADO (motivo auditable).
    - Mientras el documento está EJECUTANDO_RPA, la página se auto-refresca
      para mostrar el desenlace sin intervención del capturista.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from nicegui import run, ui

from core.models import DocumentoRegistro, EstadoDocumento, MetadatosOficio, meta_estado
from ui.layout import aplicar_tema, encabezado, obtener_config, obtener_pipeline, tiempo_relativo

logger = logging.getLogger("oficialia.ui.hitl")

#: Estados en los que el formulario es editable y se puede confirmar.
ESTADOS_EDITABLES = {EstadoDocumento.PENDIENTE_REVISION}


# ----------------------------------------------------------------------
# Validación en vivo (reglas espejo del contrato MetadatosOficio)
# ----------------------------------------------------------------------
def _validar_folio(valor: str) -> Optional[str]:
    if not valor.strip():
        return "Obligatorio (use 'S/N' si carece de folio)"
    return None


def _validar_fecha(valor: str) -> Optional[str]:
    from datetime import date

    from core.models import PATRON_FECHA_ISO

    valor = valor.strip()
    if not PATRON_FECHA_ISO.match(valor):
        return "Formato requerido: YYYY-MM-DD"
    try:
        date.fromisoformat(valor)
    except ValueError:
        return "Fecha calendario inválida"
    return None


def _validar_obligatorio(valor: str) -> Optional[str]:
    if not valor.strip():
        return "Campo obligatorio"
    return None


def _validar_asunto(valor: str) -> Optional[str]:
    if len(valor.strip()) < 5:
        return "Síntesis demasiado corta (1 a 3 oraciones)"
    return None


# ----------------------------------------------------------------------
# Página de revisión
# ----------------------------------------------------------------------
@ui.page("/revision/{doc_id}")
def pagina_revision(doc_id: str) -> None:
    """Split-screen: visor de PDF (izquierda) + formulario HITL (derecha)."""
    aplicar_tema()
    capturista: dict = {"valor": "CAPTURISTA-DEV"}
    pipeline = obtener_pipeline()
    config = obtener_config()

    documento: Optional[DocumentoRegistro] = pipeline.repo.obtener(doc_id)
    if documento is None:
        with ui.column().classes("w-full items-center q-pa-xl gap-2"):
            ui.label("404 — Documento no encontrado").classes("text-lg font-semibold text-slate-700")
            ui.button("Volver a la bandeja", icon="arrow_back").on_click(lambda: ui.navigate.to("/"))
        return

    borrador = _precargar_borrador(documento)
    estado_visto: dict = {"estado": documento.estado}

    # El encabezado es un layout de primer nivel (fuera del contenedor).
    encabezado(capturista)

    with ui.column().classes("w-full no-wrap q-pa-md gap-3").style(
        "height: calc(100vh - 48px)"
    ):

        # ---------------- Barra de contexto del documento ----------------
        with ui.row().classes("w-full items-center justify-between no-wrap gap-3"):
            with ui.row().classes("items-center gap-3 no-wrap min-w-0"):
                ui.button(icon="arrow_back").props("flat round dense color=primary").on_click(
                    lambda: ui.navigate.to("/")
                ).tooltip("Volver a la bandeja")
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(documento.nombre_archivo_original).classes(
                        "text-sm font-semibold text-slate-800 ellipsis"
                    ).style("max-width:420px")
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        info = meta_estado(documento.estado)
                        ui.badge(info.etiqueta).props(f"color={info.color}")
                        ui.label(f"Ingresado {tiempo_relativo(documento.fecha_ingesta)}").classes(
                            "text-[11px] text-slate-400"
                        )
                        if documento.preproceso:
                            ui.label(f"{documento.preproceso.num_paginas} pág.").classes(
                                "text-[11px] text-slate-400"
                            )
                        if documento.origen.value == "SCANNER_ADF":
                            ui.label("· escáner").classes("text-[11px] text-slate-400")

        # ---------------- Split-screen 50/50 ----------------
        with ui.splitter(value=50).classes("w-full flex-1 min-h-0").props("limits=30,70") as split:
            # ==== PANEL IZQUIERDO: visor de PDF ====
            with split.before:
                _panel_visor(documento)

            # ==== PANEL DERECHO: formulario + acciones ====
            with split.after:
                _panel_formulario(
                    documento, borrador, capturista, estado_visto, pipeline, config
                )

    # Auto-refresco mientras el RPA corre en segundo plano.
    if documento.estado == EstadoDocumento.EJECUTANDO_RPA:
        ui.timer(2.0, lambda: _vigilar_cambio_estado(doc_id, estado_visto))


# ----------------------------------------------------------------------
# Panel izquierdo: visor de PDF integrado
# ----------------------------------------------------------------------
def _panel_visor(documento: DocumentoRegistro) -> None:
    pagina_actual: dict = {"numero": 1}
    total_paginas = documento.preproceso.num_paginas if documento.preproceso else 1

    def _marco_html(numero: int) -> str:
        """Iframe del visor (navegación por fragmento #page=N del visor nativo)."""
        return (
            f'<iframe src="/pdf/{documento.id}#page={numero}" '
            'style="width:100%;height:100%;border:0;background:#f1f5f9" '
            'title="Visor de documento"></iframe>'
        )

    with ui.column().classes("w-full h-full no-wrap gap-2 q-pa-sm"):
        with ui.row().classes("items-center justify-between no-wrap w-full"):
            with ui.row().classes("items-center gap-1"):
                ui.button(icon="chevron_left").props("flat round dense").bind_enabled_from(
                    pagina_actual, "numero", backward=lambda n: n > 1
                ).on_click(lambda: _mover_pagina(-1))
                ui.label().bind_text_from(
                    pagina_actual, "numero", backward=lambda n: f"Pág. {n} / {total_paginas}"
                )
                ui.button(icon="chevron_right").props("flat round dense").bind_enabled_from(
                    pagina_actual, "numero", backward=lambda n: n < total_paginas
                ).on_click(lambda: _mover_pagina(1))
            ui.link("Abrir en pestaña nueva", f"/pdf/{documento.id}", new_tab=True).classes(
                "text-xs text-primary"
            )

        visor = ui.html(_marco_html(1)).classes(
            "w-full flex-1 rounded-lg border border-slate-200 overflow-hidden bg-slate-100"
        ).style("min-height:0")

        def _mover_pagina(delta: int) -> None:
            objetivo = pagina_actual["numero"] + delta
            if 1 <= objetivo <= total_paginas:
                pagina_actual["numero"] = objetivo
                visor.set_content(_marco_html(objetivo))


# ----------------------------------------------------------------------
# Panel derecho: formulario reactivo + acciones
# ----------------------------------------------------------------------
def _panel_formulario(
    documento: DocumentoRegistro,
    borrador: dict,
    capturista: dict,
    estado_visto: dict,
    pipeline,
    config,
) -> None:
    editable = documento.estado in ESTADOS_EDITABLES
    entradas: dict[str, ui.input] = {}

    def _campo(
        clave: str,
        etiqueta: str,
        *,
        placeholder: str = "",
        validador: Optional[Callable[[str], Optional[str]]] = None,
        numerico: bool = False,
        multilinea: bool = False,
    ):
        """Crea un campo enlazado al borrador (bind_value_to no es encadenable)."""
        if numerico:
            entrada = ui.number(etiqueta, placeholder=placeholder, min=0, step=1, precision=0)
            entrada.props("dense outlined color=primary")
        elif multilinea:
            entrada = ui.textarea(etiqueta, placeholder=placeholder, validation=validador)
            entrada.props("dense outlined color=primary autogrow")
        else:
            entrada = ui.input(etiqueta, placeholder=placeholder, validation=validador)
            entrada.props("dense outlined color=primary")
        entrada.classes("w-full")
        entrada.bind_value_to(borrador, clave)
        entradas[clave] = entrada
        return entrada

    with ui.scroll_area().classes("w-full h-full bg-white"):
        with ui.column().classes("w-full q-pa-md gap-3 max-w-2xl mx-auto"):

            # ---- Banners de contexto por estado ----
            _banner_estado(documento)

            # ---- Formulario ----
            ui.label("Metadatos del oficio").classes("text-sm font-semibold text-slate-700")
            ui.label(
                "Datos precargados por Gemini 2.5 Flash; la normalización (mayúsculas, "
                "folios sanitizados) se aplica al confirmar."
            ).classes("text-[11px] text-slate-400")

            _campo("numero_oficio", "Número de Oficio / Folio", placeholder="DSA-2026-089-OF o S/N", validador=_validar_folio)
            _campo("fecha_emision", "Fecha de Emisión (YYYY-MM-DD)", placeholder="2026-09-01", validador=_validar_fecha)

            with ui.row().classes("w-full items-center gap-4 no-wrap"):
                ui.label("Procedencia").classes("text-xs font-medium text-slate-600")
                ui.radio(
                    options=["HCG", "Ajena"], value=borrador["procedencia"]
                ).props("inline color=primary").bind_value_to(borrador, "procedencia")

            _campo("dependencia_area", "Dependencia / Área emisora", placeholder="DIRECCIÓN GENERAL HCG", validador=_validar_obligatorio)
            _campo("remitente_nombre", "Remitente (Firmante)", validador=_validar_obligatorio)
            _campo("remitente_cargo", "Cargo del Remitente", placeholder="NO ESPECIFICADO")
            _campo("destinatario_nombre", "Destinatario", validador=_validar_obligatorio)
            _campo("destinatario_cargo", "Cargo del Destinatario", placeholder="NO ESPECIFICADO")
            _campo("asunto", "Asunto (síntesis de 1 a 3 oraciones)", validador=_validar_asunto, multilinea=True)
            _campo("plazo_dias", "Plazo de respuesta (días)", placeholder="Vacío si no aplica", numerico=True)

            ui.switch("Contiene datos personales sensibles (LGPDPPSO)").props(
                "color=negative"
            ).bind_value_to(borrador, "contiene_datos_sensibles")

            # ---- Acciones ----
            ui.separator().classes("w-full")
            with ui.row().classes("w-full items-center justify-end gap-2 no-wrap"):
                if documento.estado == EstadoDocumento.ERROR_RPA:
                    ui.button("Reintentar RPA", icon="restart_alt").props(
                        "color=primary outline no-caps"
                    ).on_click(lambda: _reintentar_rpa())

                if documento.estado in ESTADOS_EDITABLES:
                    ui.button("Descartar", icon="delete").props(
                        "color=negative outline no-caps"
                    ).on_click(lambda: _abrir_dialogo_descartar())

                    ui.button("Confirmar y Registrar", icon="task_alt").props(
                        "color=primary no-caps"
                    ).on_click(lambda: _confirmar())

            if not editable:
                ui.label(
                    "Formulario de solo lectura: el documento no está en revisión pendiente."
                ).classes("text-[11px] text-slate-400")

    # Bloqueo del formulario cuando no es editable.
    if not editable:
        for entrada in entradas.values():
            entrada.disable()

    # ------------------------------------------------------------------
    # Acciones (bloqueantes → run.io_bound para no congelar la UI)
    # ------------------------------------------------------------------
    def _recolectar_datos() -> dict[str, Any]:
        datos = dict(borrador)
        plazo = datos.get("plazo_dias")
        if plazo is None or plazo == "":
            datos["plazo_dias"] = None
        else:
            datos["plazo_dias"] = int(float(plazo))
        return datos

    async def _confirmar() -> None:
        try:
            metadatos = MetadatosOficio.model_validate(_recolectar_datos())
        except Exception as exc:  # noqa: BLE001 — ValidationError de Pydantic
            ui.notify(f"Revise los campos marcados: {exc}", type="negative", position="top")
            return

        try:
            documento_actualizado = await run.io_bound(
                pipeline.confirmar_hitl, documento.id, metadatos, capturista["valor"].strip() or "CAPTURISTA-DEV"
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al confirmar %s", documento.id)
            ui.notify(f"No se pudo confirmar: {exc}", type="negative", position="top")
            return

        ui.notify(
            f"Registrado: {documento_actualizado.nombre_archivo_canonico}. RPA en ejecución…",
            type="positive",
            position="top",
        )
        ui.navigate.to(f"/revision/{documento.id}")

    def _abrir_dialogo_descartar() -> None:
        with ui.dialog() as dialogo, ui.card().classes("gap-3 q-pa-md"):
            ui.label("Descartar documento").classes("text-sm font-semibold text-slate-700")
            ui.label(
                "El documento pasará a DESCARTADO y el archivo se aislará en "
                "storage/04_errores/ con el motivo registrado. La acción es irreversible."
            ).classes("text-xs text-slate-500")
            motivo = ui.input("Motivo del descarte (opcional)", placeholder="Ej. documento ajeno al flujo").props(
                "dense outlined color=primary"
            ).classes("w-full")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancelar").props("flat no-caps color=grey").on_click(dialogo.close)
                ui.button("Descartar definitivamente", icon="delete").props(
                    "color=negative no-caps"
                ).on_click(lambda: _confirmar_descarte(dialogo, motivo))
        dialogo.open()

    async def _confirmar_descarte(dialogo, campo_motivo) -> None:
        dialogo.close()
        try:
            await run.io_bound(
                pipeline.descartar,
                documento.id,
                campo_motivo.value.strip(),
                capturista["valor"].strip() or "CAPTURISTA-DEV",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al descartar %s", documento.id)
            ui.notify(f"No se pudo descartar: {exc}", type="negative", position="top")
            return
        ui.notify("Documento descartado y archivado en 04_errores.", type="warning", position="top")
        ui.navigate.to("/")

    async def _reintentar_rpa() -> None:
        try:
            await run.io_bound(pipeline.reintentar_rpa, documento.id)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al reintentar RPA de %s", documento.id)
            ui.notify(f"No se pudo reintentar: {exc}", type="negative", position="top")
            return
        ui.notify("Reinyección en la Intranet en curso…", type="positive", position="top")
        ui.navigate.to(f"/revision/{documento.id}")


# ----------------------------------------------------------------------
# Banners de contexto
# ----------------------------------------------------------------------
def _banner_estado(documento: DocumentoRegistro) -> None:
    estado = documento.estado

    if estado == EstadoDocumento.ERROR_RPA and documento.rpa and documento.rpa.mensaje_error:
        with ui.card().classes("bg-rose-50 w-full shadow-none rounded-lg gap-2"):
            ui.icon("report", color="negative").classes("text-xl")
            with ui.column().classes("gap-0"):
                ui.label("El registro en la Intranet falló").classes("text-sm font-semibold text-rose-700")
                ui.label(documento.rpa.mensaje_error).classes("text-xs text-rose-600").style(
                    "white-space:pre-wrap"
                )
                if documento.rpa.captura_acuse_path:
                    ui.label(f"Evidencia del fallo: {documento.rpa.captura_acuse_path}").classes(
                        "text-[11px] text-rose-400"
                    )
        return

    if estado == EstadoDocumento.COMPLETADO:
        rpa = documento.rpa
        with ui.card().classes("bg-emerald-50 w-full shadow-none rounded-lg gap-2"):
            ui.icon("verified", color="positive").classes("text-xl")
            with ui.column().classes("gap-0"):
                ui.label("Documento registrado en la Intranet").classes(
                    "text-sm font-semibold text-emerald-700"
                )
                if rpa and rpa.folio_acuse:
                    ui.label(f"Acuse institucional: {rpa.folio_acuse}").classes("text-xs text-emerald-700")
                if rpa and rpa.captura_acuse_path:
                    ui.link("Ver captura del acuse", f"/evidencia/{documento.id}", new_tab=True).classes(
                        "text-xs text-emerald-700 underline"
                    )
                if documento.sheets.sincronizado:
                    destino = "Google Sheets" if documento.sheets.modo == "google" else "tablero local (stub)"
                    ui.label(
                        f"Sincronizado al {destino}"
                        + (f", fila {documento.sheets.fila_index}" if documento.sheets.fila_index else "")
                    ).classes("text-[11px] text-emerald-600")
                elif documento.sheets.error:
                    ui.label(f"Sheets pendiente: {documento.sheets.error}").classes(
                        "text-[11px] text-amber-600"
                    )
                if documento.ruta_espejo_json:
                    ui.label(f"JSON espejo: {documento.ruta_espejo_json}").classes(
                        "text-[11px] text-slate-400"
                    )
        return

    if estado == EstadoDocumento.DESCARTADO:
        with ui.card().classes("bg-slate-100 w-full shadow-none rounded-lg gap-2"):
            ui.icon("folder_off", color="grey-7").classes("text-xl")
            with ui.column().classes("gap-0"):
                ui.label("Documento descartado").classes("text-sm font-semibold text-slate-600")
                if documento.error_msg:
                    ui.label(documento.error_msg).classes("text-xs text-slate-500")
        return

    if estado == EstadoDocumento.EJECUTANDO_RPA:
        with ui.card().classes("bg-sky-50 w-full shadow-none rounded-lg gap-2 items-center"):
            ui.spinner("dots", size="1.2em", color="info")
            ui.label("Registrando el oficio en la Intranet Webix (RPA en ejecución)…").classes(
                "text-sm text-sky-700"
            )


# ----------------------------------------------------------------------
# Soporte
# ----------------------------------------------------------------------
def _precargar_borrador(documento: DocumentoRegistro) -> dict[str, Any]:
    """Formulario precargado con la extracción de IA (o la validada si ya hay)."""
    fuente = documento.metadatos_extraidos or documento.metadatos_validados
    if fuente is None:
        return {
            "numero_oficio": "",
            "fecha_emision": "",
            "procedencia": "Ajena",
            "dependencia_area": "",
            "remitente_nombre": "",
            "remitente_cargo": "NO ESPECIFICADO",
            "destinatario_nombre": "",
            "destinatario_cargo": "NO ESPECIFICADO",
            "asunto": "",
            "plazo_dias": None,
            "contiene_datos_sensibles": False,
        }
    return {
        "numero_oficio": fuente.numero_oficio,
        "fecha_emision": fuente.fecha_emision,
        "procedencia": fuente.procedencia.value,
        "dependencia_area": fuente.dependencia_area,
        "remitente_nombre": fuente.remitente_nombre,
        "remitente_cargo": fuente.remitente_cargo,
        "destinatario_nombre": fuente.destinatario_nombre,
        "destinatario_cargo": fuente.destinatario_cargo,
        "asunto": fuente.asunto,
        "plazo_dias": fuente.plazo_dias,
        "contiene_datos_sensibles": fuente.contiene_datos_sensibles,
    }


def _vigilar_cambio_estado(doc_id: str, estado_visto: dict) -> None:
    """Timer: recarga la vista cuando el RPA resuelve el estado terminal."""
    pipeline = obtener_pipeline()
    actual = pipeline.repo.obtener(doc_id)
    if actual is None:
        return
    if actual.estado != estado_visto["estado"]:
        ui.navigate.reload()
