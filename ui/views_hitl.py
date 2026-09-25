"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
ui/views_hitl.py — Revisión asistida Human-in-the-Loop (split-screen 50/50).

Migración del `HitlReviewView.svelte` original:

    - Panel izquierdo: visor de páginas renderizadas del documento (sirve
      imágenes individuales vía `/pdf/{id}/pagina/{n}.png`, registrada en
      main.py) con navegación de páginas, apertura en pestaña nueva y
      RESALTADO INTERACTIVO: al enfocar un campo del formulario, la zona
      del PDF donde la IA lo localizó se ilumina automáticamente (ver
      `_panel_visor`/`_resaltar_campo`, coordenadas de
      `core.models.UbicacionesCampos` producidas por `core.ai_extractor`) —
      evita buscar manualmente el dato en documentos densos o escaneados.
    - Panel derecho: formulario reactivo precargado con la extracción de
      la IA, validación en vivo campo a campo (mismas reglas del contrato
      `MetadatosOficio`) y acciones operativas:
        [Confirmar y Registrar]  → nomenclatura canónica + JSON espejo + RPA
        [Aprobar y Siguiente]    → igual que confirmar, pero encadena
                                    automáticamente el siguiente documento
                                    PENDIENTE_REVISION (modo carrusel: revisión
                                    continua sin volver a la bandeja general
                                    entre un oficio y el siguiente)
        [Descartar]              → estado terminal, archivo aislado en 04_errores
        [Reintentar RPA]         → reinyección en ERROR_RPA (sin reextraer)
        [Confirmar registro manual] → certifica a mano un folio que la
                                    Intranet SÍ registró pero el detector
                                    automático no capturó a tiempo (ver
                                    core.pipeline.FlujoDocumental.
                                    confirmar_registro_manual) — sin volver
                                    a abrir el navegador ni reenviar el
                                    formulario (evita un posible duplicado)
    - Banners de contexto: ERROR_RPA (con motivo, reintento y confirmación
      manual), COMPLETADO (folio de acuse + evidencia) y DESCARTADO (motivo
      auditable).
    - Atajos seguros: Alt+A confirma, Alt+N aprueba y pasa al siguiente
      (modo carrusel) y Alt+R abre el descarte, sin interferir con la
      captura dentro de los campos del formulario.
    - Mientras el documento está EJECUTANDO_RPA, la página se auto-refresca
      para mostrar el desenlace sin intervención manual.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from nicegui import app, run, ui

from core.models import (
    DocumentoRegistro,
    EstadoBloqueo,
    EstadoDocumento,
    EstadoRespuesta,
    MetadatosOficio,
    MetodoExtraccion,
    PeticionRespuesta,
    RegistroRespuesta,
    RespuestaOficio,
    SentidoRespuesta,
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


def _validar_fecha_recepcion(valor: str) -> Optional[str]:
    """Igual que `_validar_fecha`, pero tolerante a vacío: fecha_recepcion
    es opcional (sello ausente o ilegible, ver core.models.MetadatosOficio)."""
    from datetime import date

    from core.models import PATRON_FECHA_ISO

    valor = valor.strip()
    if not valor:
        return None
    if not PATRON_FECHA_ISO.match(valor):
        return "Formato requerido: YYYY-MM-DD (o vacío si no hay sello legible)"
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
        elif tecla == "n" and "siguiente" in acciones:
            await acciones["siguiente"]()

    # Los atajos (Alt+A confirmar, Alt+R descartar, Alt+N aprobar y siguiente)
    # ya se anuncian con tooltip() en los propios botones de acción — un
    # indicador flotante aparte terminaba superpuesto sobre "Confirmar y
    # Registrar" (detectado al verificar esta pantalla en navegador).
    ui.keyboard(_atajo_revision, repeating=False)

    # Auto-refresco mientras el RPA corre en segundo plano.
    if documento.estado == EstadoDocumento.EJECUTANDO_RPA:
        ui.timer(2.0, lambda: _vigilar_cambio_estado(doc_id, estado_visto))


# ----------------------------------------------------------------------
# Panel izquierdo: visor de PDF integrado con resaltado interactivo
# ----------------------------------------------------------------------
def _panel_visor(documento: DocumentoRegistro) -> None:
    """
    Visor de páginas renderizadas (`/pdf/{id}/pagina/{n}.png`, ver main.py)
    en vez del <iframe> nativo anterior: necesario para poder dibujar un
    recuadro de resaltado posicionado por porcentaje sobre la imagen cuando
    el revisor enfoca un campo del formulario (ver `_resaltar_campo`/
    `_quitar_resaltado` y el enganche en `_panel_formulario._campo`) — un
    <iframe> con el visor nativo del navegador no permite superponer nada.

    Paginación y resaltado corren enteramente en el cliente (JavaScript
    embebido) para que cambiar de campo no dispare una vuelta al servidor;
    Python solo necesita disparar `window.__oficialiaVisor[doc_id].resaltar(
    clave)` vía `ui.run_javascript` (ver funciones al final del módulo).
    """
    total_paginas = documento.preproceso.num_paginas if documento.preproceso else 1
    doc_id = documento.id

    cajas: dict[str, dict] = {}
    if documento.ubicaciones_campos is not None:
        for campo, caja in documento.ubicaciones_campos.model_dump().items():
            if caja is not None:
                cajas[campo] = caja

    marco = f"""
    <div style="display:flex;flex-direction:column;gap:8px;height:100%;min-height:0;">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:nowrap;
                  width:100%;padding:4px 8px;background:#fff;border-radius:8px;
                  border:1px solid #e2e8f0;">
        <div style="display:flex;align-items:center;gap:2px;">
          <button id="visor-prev-{doc_id}" title="Página anterior" class="visor-boton-{doc_id}">
            <span class="material-icons" style="font-size:18px;">chevron_left</span>
          </button>
          <span id="visor-etiqueta-{doc_id}" style="font-size:12px;font-weight:500;color:#475569;
                min-width:88px;text-align:center;">Pág. 1 / {total_paginas}</span>
          <button id="visor-next-{doc_id}" title="Página siguiente" class="visor-boton-{doc_id}">
            <span class="material-icons" style="font-size:18px;">chevron_right</span>
          </button>
        </div>
        <div style="display:flex;align-items:center;gap:8px;">
          <span style="font-size:10.5px;font-weight:600;text-transform:uppercase;
                letter-spacing:.05em;color:#94a3b8;">Documento</span>
          <a href="/pdf/{doc_id}" target="_blank" rel="noopener"
             style="font-size:12px;color:#1d4ed8;font-weight:500;text-decoration:none;">
            Abrir en pestaña nueva
          </a>
        </div>
      </div>
      <div id="visor-contenedor-{doc_id}" style="position:relative;flex:1;min-height:0;
           overflow:auto;border-radius:8px;border:1px solid #e2e8f0;background:#f1f5f9;">
        <img id="visor-img-{doc_id}" src="/pdf/{doc_id}/pagina/1.png"
             style="width:100%;display:block;">
        <div id="visor-resalte-{doc_id}" hidden style="position:absolute;
             border:2px solid #f59e0b;background:rgba(245,158,11,.22);border-radius:3px;
             box-shadow:0 0 0 3px rgba(245,158,11,.18);pointer-events:none;
             transition:top .15s ease-out,left .15s ease-out,width .15s ease-out,
             height .15s ease-out,opacity .15s ease-out;"></div>
      </div>
      <style>
        .visor-boton-{doc_id} {{
          display:flex;align-items:center;justify-content:center;width:28px;height:28px;
          border:none;background:transparent;border-radius:6px;cursor:pointer;color:#1d4ed8;
        }}
        .visor-boton-{doc_id}:hover {{ background:#eff6ff; }}
        .visor-boton-{doc_id}:disabled {{ color:#cbd5e1;cursor:default;background:transparent; }}
      </style>
    </div>
    """

    # El comportamiento va por separado vía ui.run_javascript (no como un
    # <script> embebido en el HTML de arriba): ui.html() inserta su
    # contenido por innerHTML, y un <script> insertado así el navegador
    # NUNCA lo ejecuta (restricción estándar del DOM) — de hecho ui.html()
    # rechaza de plano cualquier contenido con "</script>". run_javascript,
    # en cambio, evalúa el código directamente en el cliente, así que sí
    # corre; se dispara justo después de insertar el marco de arriba, con
    # el mismo doc_id como llave para no chocar con el visor de otra pestaña.
    guion = f"""
    (function() {{
      const docId = {json.dumps(doc_id)};
      const total = {total_paginas};
      const cajas = {json.dumps(cajas)};
      let pagina = 1;
      const img = document.getElementById("visor-img-" + docId);
      const resalte = document.getElementById("visor-resalte-" + docId);
      const etiqueta = document.getElementById("visor-etiqueta-" + docId);
      const btnPrev = document.getElementById("visor-prev-" + docId);
      const btnNext = document.getElementById("visor-next-" + docId);

      function pintarBotones() {{
        btnPrev.disabled = pagina <= 1;
        btnNext.disabled = pagina >= total;
      }}

      function ir(delta) {{
        const objetivo = pagina + delta;
        if (objetivo < 1 || objetivo > total) return;
        pagina = objetivo;
        img.src = "/pdf/" + docId + "/pagina/" + pagina + ".png";
        etiqueta.textContent = "Pág. " + pagina + " / " + total;
        resalte.hidden = true;
        pintarBotones();
      }}

      function resaltar(clave) {{
        const caja = cajas[clave];
        if (!caja) {{ resalte.hidden = true; return; }}
        if (caja.pagina !== pagina) {{ ir(caja.pagina - pagina); }}
        const x0 = Math.min(caja.x0, caja.x1), x1 = Math.max(caja.x0, caja.x1);
        const y0 = Math.min(caja.y0, caja.y1), y1 = Math.max(caja.y0, caja.y1);
        resalte.style.left = (x0 * 100) + "%";
        resalte.style.top = (y0 * 100) + "%";
        resalte.style.width = ((x1 - x0) * 100) + "%";
        resalte.style.height = ((y1 - y0) * 100) + "%";
        resalte.hidden = false;
        resalte.scrollIntoView({{block: "center", inline: "center", behavior: "smooth"}});
      }}

      function quitarResalte() {{ resalte.hidden = true; }}

      btnPrev.addEventListener("click", () => ir(-1));
      btnNext.addEventListener("click", () => ir(1));
      pintarBotones();

      window.__oficialiaVisor = window.__oficialiaVisor || {{}};
      window.__oficialiaVisor[docId] = {{ ir: ir, resaltar: resaltar, quitarResalte: quitarResalte }};
    }})();
    """

    with ui.column().classes("w-full h-full no-wrap gap-2 q-pa-sm bg-slate-50"):
        ui.html(marco).classes("w-full h-full").style("min-height:0")
    ui.run_javascript(guion)


def _resaltar_campo(doc_id: str, clave: str) -> None:
    """Pide al visor (JS embebido en `_panel_visor`) resaltar el origen
    visual de `clave` — no-op si el documento no trae ubicación para ese
    campo (ver `cajas` en `_panel_visor`)."""
    ui.run_javascript(
        f"window.__oficialiaVisor && window.__oficialiaVisor[{json.dumps(doc_id)}] "
        f"&& window.__oficialiaVisor[{json.dumps(doc_id)}].resaltar({json.dumps(clave)})"
    )


def _quitar_resaltado(doc_id: str) -> None:
    ui.run_javascript(
        f"window.__oficialiaVisor && window.__oficialiaVisor[{json.dumps(doc_id)}] "
        f"&& window.__oficialiaVisor[{json.dumps(doc_id)}].quitarResalte()"
    )


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
        numero_oficio, fecha_emision, fecha_recepcion, dependencia_area,
        remitente_nombre, remitente_cargo, destinatario_nombre,
        destinatario_cargo, asunto, plazo_dias) y enlazado para que las
        ediciones del revisor se reflejen de vuelta en `borrador`.

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
        # Resaltado interactivo en el visor PDF (ver _panel_visor/_resaltar_campo):
        # al enfocar el campo se ilumina la zona donde la IA lo localizó, sin
        # obligar al revisor a buscarlo manualmente en un documento denso o
        # escaneado; args=[] evita mandar la carga completa del evento al server.
        entrada.on("focus", lambda _=None, c=clave: _resaltar_campo(documento.id, c), [])
        entrada.on("blur", lambda _=None: _quitar_resaltado(documento.id), [])
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
            _campo(
                "fecha_recepcion",
                "Fecha de Recepción — sello (YYYY-MM-DD)",
                placeholder="2026-09-02 (vacío si no hay sello legible)",
                validador=_validar_fecha_recepcion,
            )

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
                    ).on_click(lambda: _reintentar_rpa()).tooltip(
                        "Vuelve a abrir el navegador y reenvía el formulario — úselo solo si la "
                        "Intranet NO llegó a registrar el oficio"
                    )
                    ui.button("Confirmar registro manual", icon="fact_check").props(
                        "color=positive outline no-caps"
                    ).on_click(lambda: _abrir_dialogo_registro_manual()).tooltip(
                        "Use esto si YA vio el folio en la Intranet — certifica el registro sin "
                        "volver a enviar el formulario (evita un posible duplicado)"
                    )

                if editable:
                    ui.button("Descartar", icon="delete").props(
                        "color=negative outline no-caps"
                    ).on_click(lambda: _abrir_dialogo_descartar()).tooltip("Alt+R")

                    ui.button("Confirmar y Registrar", icon="task_alt").props(
                        "color=primary outline no-caps"
                    ).on_click(lambda: _confirmar()).tooltip("Alt+A — se queda en este documento")

                    # Modo carrusel: confirma y carga automáticamente el siguiente
                    # PENDIENTE_REVISION (ver RepositorioDocumentos.siguiente_pendiente),
                    # sin obligar al revisor a volver a la bandeja general entre un
                    # oficio y el siguiente — flujo de concentración continua.
                    ui.button("Aprobar y Siguiente", icon="skip_next").props(
                        "color=primary no-caps"
                    ).on_click(lambda: _confirmar_y_siguiente()).tooltip(
                        "Alt+N — confirma y carga automáticamente el siguiente pendiente"
                    )

            if not editable and (bloqueo is None or bloqueo.adquirido):
                # Si no es editable POR EL BLOQUEO, el banner de arriba ya
                # lo explica con quién y por qué — repetir el mensaje aquí
                # sería redundante.
                ui.label(
                    "Formulario de solo lectura: el documento no está en revisión pendiente."
                ).classes("text-[11px] text-slate-400")

            # ---- Asistente de respuesta con IA (segundo flujo HITL) ----
            _panel_respuesta_ia(documento, revisor, pipeline, config)

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

    async def _validar_y_confirmar() -> Optional[DocumentoRegistro]:
        """Valida + confirma + libera el bloqueo propio; base compartida de
        `_confirmar()` y `_confirmar_y_siguiente()` — solo difiere adónde
        navegan después. Devuelve None (y ya notificó el motivo) si la
        validación o la confirmación fallaron."""
        try:
            metadatos = MetadatosOficio.model_validate(_recolectar_datos())
        except Exception as exc:  # noqa: BLE001 — ValidationError de Pydantic
            ui.notify(f"Revise los campos marcados: {exc}", type="negative", position="top")
            return None

        try:
            documento_actualizado = await run.io_bound(
                pipeline.confirmar_hitl, documento.id, metadatos, revisor["valor"].strip() or REVISOR_POR_DEFECTO
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al confirmar %s", documento.id)
            ui.notify(f"No se pudo confirmar: {exc}", type="negative", position="top")
            return None

        _liberar_bloqueo_propio()
        return documento_actualizado

    async def _confirmar() -> None:
        documento_actualizado = await _validar_y_confirmar()
        if documento_actualizado is None:
            return
        ui.notify(
            f"Registrado: {documento_actualizado.nombre_archivo_canonico}. RPA en ejecución…",
            type="positive",
            position="top",
        )
        ui.navigate.to(f"/revision/{documento.id}")

    async def _confirmar_y_siguiente() -> None:
        documento_actualizado = await _validar_y_confirmar()
        if documento_actualizado is None:
            return
        siguiente = pipeline.repo.siguiente_pendiente(documento.id)
        if siguiente is not None:
            ui.notify(
                f"Registrado: {documento_actualizado.nombre_archivo_canonico}. Siguiente documento cargado.",
                type="positive",
                position="top",
            )
            ui.navigate.to(f"/revision/{siguiente.id}")
        else:
            ui.notify(
                f"Registrado: {documento_actualizado.nombre_archivo_canonico}. "
                "No hay más documentos pendientes por revisar.",
                type="positive",
                position="top",
            )
            ui.navigate.to("/")

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

    def _abrir_dialogo_registro_manual() -> None:
        with ui.dialog() as dialogo, ui.card().classes("gap-3 q-pa-md"):
            ui.label("Confirmar registro manual").classes("text-sm font-semibold text-slate-700")
            ui.label(
                "Use esto SOLO si ya vio en la Intranet que el oficio quedó registrado (folio "
                "visible en pantalla) — el documento pasará directo a COMPLETADO con el folio que "
                "escriba abajo, sin volver a abrir el navegador ni reenviar el formulario."
            ).classes("text-xs text-slate-500")
            campo_folio = ui.input(
                "Folio institucional (tal como aparece en la Intranet)",
                placeholder="Ej. HCG-OP-2026-009821",
            ).props("dense outlined color=primary").classes("w-full")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancelar").props("flat no-caps color=grey").on_click(dialogo.close)
                ui.button("Confirmar registro", icon="fact_check").props(
                    "color=positive no-caps"
                ).on_click(lambda: _confirmar_registro_manual(dialogo, campo_folio))
        dialogo.open()

    async def _confirmar_registro_manual(dialogo, campo_folio) -> None:
        folio = campo_folio.value.strip()
        if not folio:
            ui.notify("Escriba el folio institucional para confirmar.", type="warning", position="top")
            return
        dialogo.close()
        try:
            await run.io_bound(
                pipeline.confirmar_registro_manual,
                documento.id,
                folio,
                revisor["valor"].strip() or REVISOR_POR_DEFECTO,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al confirmar registro manual de %s", documento.id)
            ui.notify(f"No se pudo confirmar: {exc}", type="negative", position="top")
            return
        ui.notify(f"Registro confirmado: folio {folio}.", type="positive", position="top")
        ui.navigate.to(f"/revision/{documento.id}")

    acciones: dict[str, Callable] = {}
    if editable:
        acciones = {
            "aprobar": _confirmar,
            "rechazar": _abrir_dialogo_descartar,
            "siguiente": _confirmar_y_siguiente,
        }
    return acciones


# ----------------------------------------------------------------------
# Asistente de respuesta a oficios con IA (segundo flujo HITL, ver
# core/ai_responder.py y core/pipeline.py::FlujoDocumental.
# generar_borrador_respuesta / actualizar_borrador_respuesta /
# generar_documento_respuesta). Sigue el mismo idioma que el resto de la
# pantalla: cada acción que muta datos vuelve a navegar a la misma URL
# (en vez de un refresco parcial) para redibujar desde la fuente de verdad
# en SQLite, igual que _confirmar()/_confirmar_descarte()/_reintentar_rpa()
# más arriba.
# ----------------------------------------------------------------------
_ETIQUETAS_SENTIDO: dict[str, str] = {
    SentidoRespuesta.ATENCION_FAVORABLE.value: "Atención favorable",
    SentidoRespuesta.SOLICITUD_PRORROGA.value: "Solicitud de prórroga",
    SentidoRespuesta.REQUERIMIENTO_INFO.value: "Requerimiento de información",
    SentidoRespuesta.INCOMPETENCIA_TURNO.value: "Incompetencia / turno",
    SentidoRespuesta.NEGATIVA_FUNDADA.value: "Negativa fundada",
    SentidoRespuesta.PERSONALIZADA.value: "Personalizada",
}


def _panel_respuesta_ia(documento: DocumentoRegistro, revisor: dict, pipeline, config) -> None:
    """
    Disponible para cualquier oficio que ya tenga metadatos (extraídos o
    validados): redactar la contestación es una acción independiente de
    confirmar la extracción original, así que NO depende de `editable` ni
    del estado del documento (tiene sentido incluso ya COMPLETADO).
    """
    oficio_origen = documento.metadatos_validados or documento.metadatos_extraidos
    if oficio_origen is None:
        return

    with ui.expansion("Generar Respuesta al Oficio", icon="description").classes("w-full").props("dense"):
        with ui.column().classes("w-full gap-3 q-pa-sm"):
            ui.label(
                "Genera un borrador de contestación institucional; usted lo revisa, edita y "
                "aprueba antes de descargar el Word — nada se envía ni se firma de forma automática."
            ).classes("text-[11px] text-slate-400")

            _formulario_generacion(documento, revisor, pipeline)

            historial = pipeline.repo.listar_respuestas(documento.id)
            if historial:
                ui.separator().classes("w-full")
                for registro in historial:
                    _tarjeta_borrador_respuesta(registro, documento, revisor, pipeline, config)


def _formulario_generacion(documento: DocumentoRegistro, revisor: dict, pipeline) -> None:
    """Directrices del funcionario + botón [Generar Borrador]."""
    peticion_estado: dict[str, str] = {
        "sentido": SentidoRespuesta.ATENCION_FAVORABLE.value,
        "fundamento_legal": "",
        "instrucciones_adicionales": "",
        "firmante_nombre": "Titular de la Unidad Administrativa",
        "firmante_cargo": "Director / Encargado de Área",
    }

    with ui.row().classes("w-full gap-3 no-wrap flex-wrap"):
        ui.select(
            options=_ETIQUETAS_SENTIDO, value=peticion_estado["sentido"], label="Sentido de la contestación"
        ).props("dense outlined color=primary").classes("flex-1").style("min-width:220px").bind_value_to(
            peticion_estado, "sentido"
        )
        ui.input("Fundamento legal (opcional)", placeholder="Ej. Art. 8 Constitucional").props(
            "dense outlined color=primary"
        ).classes("flex-1").style("min-width:220px").bind_value_to(peticion_estado, "fundamento_legal")

    with ui.row().classes("w-full gap-3 no-wrap flex-wrap"):
        ui.input("Firma: nombre", value=peticion_estado["firmante_nombre"]).props(
            "dense outlined color=primary"
        ).classes("flex-1").style("min-width:220px").bind_value_to(peticion_estado, "firmante_nombre")
        ui.input("Firma: cargo", value=peticion_estado["firmante_cargo"]).props(
            "dense outlined color=primary"
        ).classes("flex-1").style("min-width:220px").bind_value_to(peticion_estado, "firmante_cargo")

    ui.textarea(
        "Instrucciones / argumentos clave (opcional)",
        placeholder="Ej. Indicar que el trámite está listo para recogerse el viernes de 9 a 14 hrs.",
    ).props("dense outlined color=primary autogrow").classes("w-full").bind_value_to(
        peticion_estado, "instrucciones_adicionales"
    )

    async def _generar() -> None:
        try:
            peticion = PeticionRespuesta(
                sentido=SentidoRespuesta(peticion_estado["sentido"]),
                instrucciones_adicionales=peticion_estado["instrucciones_adicionales"],
                fundamento_legal=peticion_estado["fundamento_legal"],
                firmante_nombre=peticion_estado["firmante_nombre"],
                firmante_cargo=peticion_estado["firmante_cargo"],
            )
        except Exception as exc:  # noqa: BLE001 — ValidationError de Pydantic
            ui.notify(f"Revise los campos: {exc}", type="negative", position="top")
            return

        ui.notify("Generando borrador…", type="info", position="top")
        try:
            await run.io_bound(pipeline.generar_borrador_respuesta, documento.id, peticion)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Fallo al generar borrador de respuesta para %s", documento.id)
            ui.notify(f"No se pudo generar el borrador: {exc}", type="negative", position="top")
            return
        ui.notify("Borrador generado.", type="positive", position="top")
        ui.navigate.to(f"/revision/{documento.id}")

    ui.button("Generar Borrador", icon="post_add").props("color=primary no-caps").on_click(_generar)


def _tarjeta_borrador_respuesta(
    registro: RegistroRespuesta,
    documento: DocumentoRegistro,
    revisor: dict,
    pipeline,
    config,
) -> None:
    """Una contestación generada: editable mientras esté en BORRADOR;
    de solo lectura + enlace de descarga una vez APROBADA."""
    contenido = registro.respuesta
    campo_estado: dict[str, str] = {
        "destinatario_nombre": contenido.destinatario_nombre,
        "destinatario_cargo": contenido.destinatario_cargo or "",
        "destinatario_dependencia": contenido.destinatario_dependencia or "",
        "asunto": contenido.asunto,
        "cuerpo_respuesta": contenido.cuerpo_respuesta,
        "despedida": contenido.despedida,
        "ccp": ", ".join(contenido.ccp),
    }
    editable = registro.estado == EstadoRespuesta.BORRADOR

    with ui.card().classes("w-full shadow-none border border-slate-200 rounded-lg gap-2 q-pa-sm"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            ui.label(_ETIQUETAS_SENTIDO.get(registro.sentido.value, registro.sentido.value)).classes(
                "text-xs font-semibold text-slate-700"
            )
            aprobada = registro.estado == EstadoRespuesta.APROBADA
            ui.label("Aprobada" if aprobada else "Borrador").classes(
                "rounded-full px-2 py-0.5 text-[10px] font-medium"
            ).style(f"background:{'#d1fae5' if aprobada else '#fef3c7'};color:{'#047857' if aprobada else '#92400e'}")
        ui.label(
            f"Generado {tiempo_relativo(registro.fecha_creacion)} · firma: "
            f"{registro.firmante_nombre} ({registro.firmante_cargo})"
        ).classes("text-[10.5px] text-slate-400")

        def _campo_resp(clave: str, etiqueta: str, *, multilinea: bool = False):
            if multilinea:
                entrada = ui.textarea(etiqueta, value=campo_estado[clave]).props(
                    "dense outlined color=primary autogrow"
                )
            else:
                entrada = ui.input(etiqueta, value=campo_estado[clave]).props("dense outlined color=primary")
            entrada.classes("w-full")
            entrada.bind_value_to(campo_estado, clave)
            if not editable:
                entrada.disable()
            return entrada

        _campo_resp("destinatario_nombre", "Destinatario")
        with ui.row().classes("w-full gap-2 no-wrap"):
            _campo_resp("destinatario_cargo", "Cargo del destinatario")
            _campo_resp("destinatario_dependencia", "Dependencia")
        _campo_resp("asunto", "Asunto")
        _campo_resp("cuerpo_respuesta", "Cuerpo de la respuesta", multilinea=True)
        _campo_resp("despedida", "Despedida")
        _campo_resp("ccp", "C.c.p. (separados por coma)")

        if not editable:
            with ui.row().classes("w-full justify-end"):
                ui.link("Descargar Word", f"/respuesta/{registro.id}/docx", new_tab=True).classes(
                    "text-xs text-primary font-medium"
                )
            return

        def _recolectar() -> RespuestaOficio:
            return RespuestaOficio(
                destinatario_nombre=campo_estado["destinatario_nombre"],
                destinatario_cargo=campo_estado["destinatario_cargo"] or None,
                destinatario_dependencia=campo_estado["destinatario_dependencia"] or None,
                asunto=campo_estado["asunto"],
                cuerpo_respuesta=campo_estado["cuerpo_respuesta"],
                despedida=campo_estado["despedida"],
                ccp=[parte.strip() for parte in campo_estado["ccp"].split(",") if parte.strip()],
            )

        async def _guardar() -> None:
            try:
                editado = _recolectar()
            except Exception as exc:  # noqa: BLE001 — ValidationError de Pydantic
                ui.notify(f"Revise los campos: {exc}", type="negative", position="top")
                return
            try:
                await run.io_bound(
                    pipeline.actualizar_borrador_respuesta,
                    registro.id,
                    editado,
                    version_esperada=registro.version,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Fallo al guardar la edición de la respuesta %s", registro.id)
                ui.notify(f"No se pudo guardar: {exc}", type="negative", position="top")
                return
            ui.notify("Cambios guardados.", type="positive", position="top")
            ui.navigate.to(f"/revision/{documento.id}")

        async def _aprobar() -> None:
            try:
                editado = _recolectar()
            except Exception as exc:  # noqa: BLE001 — ValidationError de Pydantic
                ui.notify(f"Revise los campos: {exc}", type="negative", position="top")
                return
            try:
                # Guarda cualquier último ajuste ANTES de fijarlo en el .docx.
                await run.io_bound(
                    pipeline.actualizar_borrador_respuesta,
                    registro.id,
                    editado,
                    version_esperada=registro.version,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Fallo al guardar antes de aprobar la respuesta %s", registro.id)
                ui.notify(f"No se pudo guardar: {exc}", type="negative", position="top")
                return

            plantilla = config.respuestas_plantilla_path
            try:
                await run.io_bound(
                    pipeline.generar_documento_respuesta,
                    registro.id,
                    revisor["valor"].strip() or REVISOR_POR_DEFECTO,
                    plantilla_path=str(plantilla) if plantilla else None,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Fallo al aprobar/generar el Word de la respuesta %s", registro.id)
                ui.notify(f"No se pudo aprobar: {exc}", type="negative", position="top")
                return
            ui.notify("Respuesta aprobada: documento Word generado.", type="positive", position="top")
            ui.navigate.to(f"/revision/{documento.id}")

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Guardar cambios", icon="save").props("flat no-caps color=primary").on_click(_guardar)
            ui.button("Aprobar y generar Word", icon="description").props("color=primary no-caps").on_click(_aprobar)


# ----------------------------------------------------------------------
# Historial de auditoría
# ----------------------------------------------------------------------
_ETIQUETA_ACCION = {
    "CONFIRMAR": "Confirmó y registró",
    "CONFIRMAR_LOTE": "Confirmó en lote",
    "DESCARTAR": "Descartó",
    "REINTENTAR_RPA": "Reintentó el registro RPA",
    "CONFIRMAR_REGISTRO_MANUAL": "Certificó el registro a mano",
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
                ui.label(
                    "¿El oficio SÍ quedó registrado en la Intranet (vio un folio en pantalla)? Use "
                    "«Confirmar registro manual» en vez de «Reintentar RPA»: reintentar reenvía el "
                    "formulario y puede duplicar el registro."
                ).classes("text-[11px] text-rose-400 mt-1")
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
            "fecha_recepcion": "",
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
        "fecha_recepcion": fuente.fecha_recepcion or "",
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
