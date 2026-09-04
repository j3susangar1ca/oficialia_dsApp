"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
ui/views_hitl.py — Revisión asistida Human-in-the-Loop (split-screen 50/50).

Migración del `HitlReviewView.svelte` original:

    - Panel izquierdo: visor de PDF integrado y persistente (sirve el archivo actual del
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
    - Atajos seguros: Alt+A confirma y Alt+R abre el descarte, sin interferir
      con la captura dentro de los campos del formulario.
    - Mientras el documento está EJECUTANDO_RPA, la página se auto-refresca
      para mostrar el desenlace sin intervención manual.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from nicegui import app, run, ui

from core.models import (
    DocumentoRegistro,
    EstadoBloqueo,
    EstadoDocumento,
    MetadatosOficio,
    MetodoExtraccion,
    meta_estado,
)
from ui.layout import (
    REVISOR_POR_DEFECTO,
    aplicar_tema,
    encabezado,
    estilo_badge,
    obtener_config,
    obtener_pipeline,
    tiempo_relativo,
)

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
    # app.storage.user: mismo revisor que ya escribió en la bandeja, sin
    # retiparlo (ver pagina_bandeja) — y la identidad estable que ata el
    # bloqueo de edición concurrente más abajo. setdefault: en un navegador
    # nuevo "valor" no existe todavía (ver la misma nota en pagina_bandeja).
    revisor = app.storage.user
    revisor.setdefault("valor", "")
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

    # ---------------- Bloqueo de edición concurrente ----------------
    # Solo tiene sentido intentarlo en estados editables: un documento ya
    # COMPLETADO/DESCARTADO/en RPA es de solo lectura de todas formas, sin
    # necesidad de bloqueo. Se captura `nombre_revisor_bloqueo` UNA vez
    # aquí (no se re-deriva de revisor["valor"] en cada acción): si el
    # revisor cambia el nombre en el encabezado a media revisión, liberar/
    # renovar debe seguir usando la MISMA identidad con la que se adquirió
    # — RepositorioDocumentos.liberar_bloqueo exige que coincida.
    bloqueo: Optional[EstadoBloqueo] = None
    nombre_revisor_bloqueo: Optional[str] = None
    if documento.estado in ESTADOS_EDITABLES:
        nombre_revisor_bloqueo = revisor["valor"].strip() or REVISOR_POR_DEFECTO
        bloqueo = pipeline.repo.adquirir_bloqueo(
            doc_id, nombre_revisor_bloqueo, ttl_minutos=config.hitl_lock_ttl_min
        )
        if bloqueo.adquirido:
            # Heartbeat: bien por debajo del TTL para tolerar latencia de
            # red sin que el bloqueo venza mientras la pantalla sigue
            # abierta y en uso.
            intervalo_heartbeat = max(5.0, min(30.0, config.hitl_lock_ttl_min * 60 / 2))
            ui.timer(
                intervalo_heartbeat,
                lambda: pipeline.repo.renovar_bloqueo(
                    doc_id, nombre_revisor_bloqueo, ttl_minutos=config.hitl_lock_ttl_min
                ),
            )
            # Cierre de pestaña, navegación fuera de la página o pérdida de
            # conexión: red de seguridad además de la liberación explícita
            # en _confirmar()/_confirmar_descarte() (ver _panel_formulario)
            # — para cuando el revisor simplemente se va sin confirmar ni
            # descartar.
            ui.context.client.on_disconnect(
                lambda: pipeline.repo.liberar_bloqueo(doc_id, nombre_revisor_bloqueo)
            )

    # El encabezado es un layout de primer nivel (fuera del contenedor).
    encabezado(revisor)

    with ui.column().classes("w-full no-wrap q-pa-md gap-3").style(
        "height: calc(100vh - 48px)"
    ):

        # ---------------- Barra de contexto del documento ----------------
        with ui.row().classes(
            "w-full items-center justify-between no-wrap gap-3 bg-white rounded-xl border "
            "border-slate-200 q-pa-sm"
        ):
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
                        ui.label(info.etiqueta).classes(
                            "rounded-full px-2.5 py-0.5 text-[11px] font-medium"
                        ).style(estilo_badge(info.color))
                        ui.label(f"Ingresado {tiempo_relativo(documento.fecha_ingesta)}").classes(
                            "text-[11px] text-slate-400"
                        )
                        if documento.preproceso:
                            ui.label(f"{documento.preproceso.num_paginas} pág.").classes(
                                "text-[11px] text-slate-400"
                            )
                        if documento.origen.value == "SCANNER_ADF":
                            ui.label("· escáner").classes("text-[11px] text-slate-400")

        # El PDF recibe más espacio, mientras el panel lateral conserva un
        # ancho cómodo para validar sin desplazamiento horizontal.
        with ui.splitter(value=58).classes(
            "w-full flex-1 min-h-0 rounded-xl border border-slate-200 overflow-hidden"
        ).props("limits=38,68") as split:
            # ==== PANEL IZQUIERDO: visor de PDF ====
            with split.before:
                _panel_visor(documento)

            # ==== PANEL DERECHO: formulario + acciones ====
            with split.after:
                acciones = _panel_formulario(
                    documento, borrador, revisor, estado_visto, pipeline, config,
                    bloqueo, nombre_revisor_bloqueo,
                )

    async def _atajo_revision(evento) -> None:
        """Acciones rápidas, ignoradas automáticamente cuando se edita un campo."""
        if not evento.action.keydown or evento.action.repeat or not evento.modifiers.alt:
            return
        tecla = evento.key.name.lower()
        if tecla == "a" and "aprobar" in acciones:
            await acciones["aprobar"]()
        elif tecla == "r" and "rechazar" in acciones:
            acciones["rechazar"]()

    # Los atajos (Alt+A confirmar, Alt+R descartar) ya se anuncian con
    # tooltip() en los propios botones de acción — un indicador flotante
    # aparte terminaba superpuesto sobre "Confirmar y Registrar"
    # (detectado al verificar esta pantalla en navegador).
    ui.keyboard(_atajo_revision, repeating=False)

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
            f'<iframe src="/pdf/{documento.id}#page={numero}&view=FitH" '
            'style="width:100%;height:100%;border:0;background:#f1f5f9" loading="eager" '
            'title="Visor de documento"></iframe>'
        )

    with ui.column().classes("w-full h-full no-wrap gap-2 q-pa-sm bg-slate-50"):
        with ui.row().classes(
            "items-center justify-between no-wrap w-full q-px-sm q-py-xs bg-white "
            "rounded-lg border border-slate-200"
        ):
            with ui.row().classes("items-center gap-1"):
                ui.button(icon="chevron_left").props("flat round dense").bind_enabled_from(
                    pagina_actual, "numero", backward=lambda n: n > 1
                ).on_click(lambda: _mover_pagina(-1))
                ui.label().classes("text-xs font-medium text-slate-600").bind_text_from(
                    pagina_actual, "numero", backward=lambda n: f"Pág. {n} / {total_paginas}"
                )
                ui.button(icon="chevron_right").props("flat round dense").bind_enabled_from(
                    pagina_actual, "numero", backward=lambda n: n < total_paginas
                ).on_click(lambda: _mover_pagina(1))
            with ui.row().classes("items-center gap-2"):
                ui.label("Documento").classes(
                    "text-[10.5px] font-semibold uppercase tracking-wider text-slate-400"
                )
                ui.link("Abrir en pestaña nueva", f"/pdf/{documento.id}", new_tab=True).classes(
                    "text-xs text-primary font-medium"
                )

        visor = ui.html(_marco_html(1)).classes(
            "w-full flex-1 rounded-lg border border-slate-200 overflow-hidden bg-slate-100 shadow-sm"
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
    revisor: dict,
    estado_visto: dict,
    pipeline,
    config,
    bloqueo: Optional[EstadoBloqueo] = None,
    nombre_revisor_bloqueo: Optional[str] = None,
) -> dict[str, Callable]:
    # bloqueo es None cuando el estado no es editable en primer lugar (ver
    # pagina_revision: ahí ni se intenta adquirir) — en ese caso el propio
    # estado ya decide "no editable" sin que el bloqueo lo afecte más.
    editable = documento.estado in ESTADOS_EDITABLES and (bloqueo is None or bloqueo.adquirido)
    entradas: dict[str, ui.input] = {}

    def _liberar_bloqueo_propio() -> None:
        """Libera el bloqueo de inmediato al confirmar/descartar (además
        del Client.on_disconnect en pagina_revision, que cubre salir sin
        confirmar ni descartar)."""
        if nombre_revisor_bloqueo is not None:
            pipeline.repo.liberar_bloqueo(documento.id, nombre_revisor_bloqueo)

    def _campo(
        clave: str,
        etiqueta: str,
        *,
        placeholder: str = "",
        validador: Optional[Callable[[str], Optional[str]]] = None,
        numerico: bool = False,
        multilinea: bool = False,
    ):
        """
        Crea un campo precargado con el valor ya presente en `borrador`
        (`_precargar_borrador`, con los nombres de columna del esquema v2:
        numero_oficio, fecha_emision, dependencia_area, remitente_nombre,
        remitente_cargo, destinatario_nombre, destinatario_cargo, asunto,
        plazo_dias) y enlazado para que las ediciones del revisor se
        reflejen de vuelta en `borrador`.

        OJO: `bind_value_to` es una sincronización de UNA sola vía
        (widget → borrador) que se dispara de inmediato al enlazar (no
        cuando cambia `borrador`) — sin pasar `value=` al construir el
        widget, esa sincronización inicial pisaría el dato ya precargado en
        `borrador` con el valor vacío por defecto del widget, dejando el
        formulario en blanco pese a que la extracción automática sí
        completó los metadatos. Por eso el valor inicial se toma
        explícitamente de `borrador` aquí
        (bind_value_to no es encadenable, de ahí que no se use bind_value).
        """
        valor_inicial = borrador.get(clave)
        if numerico:
            entrada = ui.number(etiqueta, value=valor_inicial, placeholder=placeholder, min=0, step=1, precision=0)
            entrada.props("dense outlined color=primary")
        elif multilinea:
            entrada = ui.textarea(etiqueta, value=valor_inicial or "", placeholder=placeholder, validation=validador)
            entrada.props("dense outlined color=primary autogrow")
        else:
            entrada = ui.input(etiqueta, value=valor_inicial or "", placeholder=placeholder, validation=validador)
            entrada.props("dense outlined color=primary")
        entrada.classes("w-full")
        entrada.bind_value_to(borrador, clave)
        entradas[clave] = entrada
        return entrada

    with ui.scroll_area().classes("w-full h-full bg-white"):
        with ui.column().classes("w-full q-pa-md gap-3 max-w-2xl mx-auto"):

            # ---- Banners de contexto por estado ----
            if bloqueo is not None and not bloqueo.adquirido:
                _banner_bloqueo_ajeno(bloqueo)
            _banner_estado(documento)
            _banner_extraccion_heuristica(documento)

            # ---- Formulario ----
            ui.label("METADATOS DEL OFICIO").classes(
                "text-[11px] font-semibold uppercase tracking-wider text-slate-400"
            )
            ui.label(
                "Campos precargados automáticamente; la normalización (mayúsculas, "
                "folios sanitizados) se aplica al confirmar."
            ).classes("text-[11px] text-slate-400 -mt-2")

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

            ui.switch(
                "Contiene datos personales sensibles (LGPDPPSO)",
                value=borrador.get("contiene_datos_sensibles", False),
            ).props("color=negative").bind_value_to(borrador, "contiene_datos_sensibles")

            # ---- Acciones ----
            ui.separator().classes("w-full")
            with ui.row().classes("w-full items-center justify-end gap-2 no-wrap").style(
                "position:sticky;bottom:0;background:white;padding:10px 0;z-index:1;"
                "border-top:1px solid #f1f5f9"
            ):
                if documento.estado == EstadoDocumento.ERROR_RPA:
                    ui.button("Reintentar RPA", icon="restart_alt").props(
                        "color=primary outline no-caps"
                    ).on_click(lambda: _reintentar_rpa())

                if editable:
                    ui.button("Descartar", icon="delete").props(
                        "color=negative outline no-caps"
                    ).on_click(lambda: _abrir_dialogo_descartar()).tooltip("Alt+R")

                    ui.button("Confirmar y Registrar", icon="task_alt").props(
                        "color=primary no-caps"
                    ).on_click(lambda: _confirmar()).tooltip("Alt+A")

            if not editable and (bloqueo is None or bloqueo.adquirido):
                # Si no es editable POR EL BLOQUEO, el banner de arriba ya
                # lo explica con quién y por qué — repetir el mensaje aquí
                # sería redundante.
                ui.label(
                    "Formulario de solo lectura: el documento no está en revisión pendiente."
                ).classes("text-[11px] text-slate-400")

            # ---- Historial de auditoría ----
            _panel_auditoria(documento, pipeline)

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
                pipeline.confirmar_hitl, documento.id, metadatos, revisor["valor"].strip() or REVISOR_POR_DEFECTO
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al confirmar %s", documento.id)
            ui.notify(f"No se pudo confirmar: {exc}", type="negative", position="top")
            return

        _liberar_bloqueo_propio()
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
                revisor["valor"].strip() or REVISOR_POR_DEFECTO,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al descartar %s", documento.id)
            ui.notify(f"No se pudo descartar: {exc}", type="negative", position="top")
            return
        _liberar_bloqueo_propio()
        ui.notify("Documento descartado y archivado en 04_errores.", type="warning", position="top")
        ui.navigate.to("/")

    async def _reintentar_rpa() -> None:
        try:
            await run.io_bound(
                pipeline.reintentar_rpa, documento.id, revisor["valor"].strip() or REVISOR_POR_DEFECTO
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al reintentar RPA de %s", documento.id)
            ui.notify(f"No se pudo reintentar: {exc}", type="negative", position="top")
            return
        ui.notify("Reinyección en la Intranet en curso…", type="positive", position="top")
        ui.navigate.to(f"/revision/{documento.id}")

    acciones: dict[str, Callable] = {}
    if editable:
        acciones = {"aprobar": _confirmar, "rechazar": _abrir_dialogo_descartar}
    return acciones


# ----------------------------------------------------------------------
# Historial de auditoría
# ----------------------------------------------------------------------
_ETIQUETA_ACCION = {
    "CONFIRMAR": "Confirmó y registró",
    "CONFIRMAR_LOTE": "Confirmó en lote",
    "DESCARTAR": "Descartó",
    "REINTENTAR_RPA": "Reintentó el registro RPA",
}


def _panel_auditoria(documento: DocumentoRegistro, pipeline) -> None:
    """
    Historial de acciones HITL sobre este documento: quién, qué acción y
    qué campos corrigió respecto de lo que se extrajo automáticamente (ver
    database.py::listar_auditoria, core.models.diferencia_metadatos).
    Colapsado por defecto para no saturar la pantalla en el caso normal
    (documento aún sin ninguna acción registrada).
    """
    try:
        historial = pipeline.repo.listar_auditoria(documento.id)
    except Exception:  # noqa: BLE001 — el historial es informativo, nunca debe romper la vista
        logger.exception("No se pudo cargar el historial de auditoría de %s", documento.id)
        return
    if not historial:
        return

    with ui.expansion(f"Historial de auditoría ({len(historial)})", icon="history").classes(
        "w-full text-xs"
    ).props("dense"):
        with ui.column().classes("w-full gap-2 q-pa-sm"):
            for entrada in historial:
                with ui.column().classes("gap-0.5 border-l-2 border-slate-200 pl-2"):
                    ui.label(
                        f"{_ETIQUETA_ACCION.get(entrada.accion.value, entrada.accion.value)} — "
                        f"{entrada.revisor_usuario_id} · {tiempo_relativo(entrada.fecha)}"
                    ).classes("text-xs font-medium text-slate-700")
                    if entrada.campos_modificados:
                        for campo, cambio in entrada.campos_modificados.items():
                            ui.label(
                                f"  {campo}: “{cambio.get('anterior') or '—'}” → “{cambio.get('nuevo') or '—'}”"
                            ).classes("text-[11px] text-slate-500")


# ----------------------------------------------------------------------
# Banners de contexto
# ----------------------------------------------------------------------
def _banner_bloqueo_ajeno(bloqueo: EstadoBloqueo) -> None:
    """
    Otro revisor tiene este documento abierto para editar AHORA MISMO (ver
    RepositorioDocumentos.adquirir_bloqueo) — no un error, solo una
    advertencia temprana para que dos personas no editen el mismo oficio a
    la vez sin saberlo y una termine sobrescribiendo el trabajo de la otra.
    El formulario queda de solo lectura (ver `editable` en
    _panel_formulario) hasta que esa persona libere el bloqueo (confirma,
    descarta o cierra la pestaña) o venza por inactividad.
    """
    with ui.card().classes("bg-amber-50 border-l-4 border-amber-400 w-full shadow-none rounded-lg gap-2"):
        ui.icon("lock", color="amber-9").classes("text-xl")
        with ui.column().classes("gap-0"):
            ui.label(f"En revisión por {bloqueo.poseido_por}").classes(
                "text-sm font-semibold text-amber-900"
            )
            ui.label(
                "Alguien más tiene este documento abierto para editar ahora mismo. Puede "
                "consultarlo en modo lectura; si esa persona cierra la pantalla o pasan unos "
                "minutos sin actividad, el bloqueo se libera solo y podrá volver a intentarlo."
            ).classes("text-xs text-amber-800")


def _banner_extraccion_heuristica(documento: DocumentoRegistro) -> None:
    """
    Advertencia imposible de pasar por alto cuando el formulario NO viene
    de la extracción automática principal sino del respaldo por regex
    (core.heuristic_extractor — se activa cuando la extracción principal
    falla, ver core.pipeline). Todo lo precargado salvo quizá número de
    oficio/fecha son placeholders genéricos: el revisor debe completar/
    verificar campo por campo, nunca solo confirmar.
    """
    if documento.extraccion_metodo != MetodoExtraccion.HEURISTICA_FALLBACK:
        return
    with ui.card().classes(
        "bg-orange-50 border-l-4 border-orange-400 w-full shadow-none rounded-lg gap-2"
    ):
        ui.icon("warning", color="orange-9").classes("text-xl")
        with ui.column().classes("gap-0"):
            ui.label("Extracción de respaldo — requiere revisión completa").classes(
                "text-sm font-semibold text-orange-900"
            )
            ui.label(
                "Este formulario NO se completó automáticamente: se rescató número de oficio y "
                "fecha por texto plano (cuando fue posible) y todo lo demás quedó en un valor "
                "genérico (\"NO ESPECIFICADO\", \"ILEGIBLE\"). Revise y complete CADA campo contra "
                "el visor de PDF antes de confirmar — no se limite a corroborar lo precargado."
            ).classes("text-xs text-orange-800")


def _banner_estado(documento: DocumentoRegistro) -> None:
    estado = documento.estado

    if estado == EstadoDocumento.ERROR_RPA and documento.rpa and documento.rpa.mensaje_error:
        with ui.card().classes("bg-rose-50 border-l-4 border-rose-400 w-full shadow-none rounded-lg gap-2"):
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
        with ui.card().classes(
            "bg-emerald-50 border-l-4 border-emerald-400 w-full shadow-none rounded-lg gap-2"
        ):
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
                    destino = "a Google Sheets" if documento.sheets.modo == "google" else "al respaldo local"
                    ui.label(
                        f"Sincronizado {destino}"
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
        with ui.card().classes("bg-slate-100 border-l-4 border-slate-300 w-full shadow-none rounded-lg gap-2"):
            ui.icon("folder_off", color="grey-7").classes("text-xl")
            with ui.column().classes("gap-0"):
                ui.label("Documento descartado").classes("text-sm font-semibold text-slate-600")
                if documento.error_msg:
                    ui.label(documento.error_msg).classes("text-xs text-slate-500")
        return

    if estado == EstadoDocumento.EJECUTANDO_RPA:
        with ui.card().classes(
            "bg-sky-50 border-l-4 border-sky-400 w-full shadow-none rounded-lg gap-2 items-center"
        ):
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
