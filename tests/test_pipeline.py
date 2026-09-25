"""
tests/test_pipeline.py — Integración: ingesta completa con extractor IA
con doble (fake), sin red ni credenciales reales. Cubre el camino de
respaldo heurístico (core.heuristic_extractor) que core.pipeline invoca
cuando la IA falla.
"""

from __future__ import annotations

import uuid

import pymupdf
import pytest

from core.ai_extractor import ErrorExtraccionIA
from core.models import (
    AccionAuditoria,
    DocumentoRegistro,
    EstadoDocumento,
    EstadoRespuesta,
    MetadatosOficio,
    MetodoExtraccion,
    OrigenIngesta,
    PeticionRespuesta,
    Procedencia,
    RespuestaOficio,
    SentidoRespuesta,
    UbicacionesCampos,
)
from core.pipeline import FlujoDocumental


def _pdf_con_oficio() -> bytes:
    doc = pymupdf.open()
    pagina = doc.new_page()
    pagina.insert_text((72, 72), "OFICIO No. DSA-2026-777-OF", fontsize=14)
    pagina.insert_text((72, 100), "15 de agosto de 2026", fontsize=11)
    buffer = doc.tobytes()
    doc.close()
    return buffer


def _pdf_en_blanco() -> bytes:
    doc = pymupdf.open()
    doc.new_page()
    buffer = doc.tobytes()
    doc.close()
    return buffer


class _ExtractorFalso:
    """Doble de ExtractorMetadatos: simula éxito o un código de error dado."""

    def __init__(self, codigo_error: str | None = None):
        self.codigo_error = codigo_error

    def extraer_de_paginas(self, *args, **kwargs):
        if self.codigo_error:
            raise ErrorExtraccionIA(self.codigo_error, f"Fallo simulado ({self.codigo_error})")
        raise AssertionError("Este doble no simula el camino de éxito de la IA")


class _ExtractorCapturaPistas:
    """Doble de ExtractorMetadatos que simula ÉXITO de la IA y registra los
    kwargs recibidos — para verificar que core.pipeline efectivamente
    ejecuta el preprocesamiento heurístico y lo reenvía a
    extraer_de_paginas ANTES de la llamada (no lo descarta, no lo pierde)."""

    def __init__(self):
        self.llamadas: list[dict] = []

    def extraer_de_paginas(self, paginas, *, anio_contexto, textos_ocr=None, pistas_heuristicas=None):
        self.llamadas.append({
            "paginas": paginas,
            "anio_contexto": anio_contexto,
            "textos_ocr": textos_ocr,
            "pistas_heuristicas": pistas_heuristicas,
        })
        metadatos = MetadatosOficio(
            numero_oficio="DSA-2026-777-OF",
            fecha_emision="2026-08-15",
            procedencia=Procedencia.AJENA,
            dependencia_area="NO ESPECIFICADO",
            remitente_nombre="ALGUIEN",
            destinatario_nombre="ALGUIEN MAS",
            asunto="Asunto de prueba con longitud suficiente para el contrato.",
        )
        return metadatos, UbicacionesCampos()


@pytest.fixture
def flujo(repositorio, gestor_archivos, configuracion):
    def _crear(extractor, redactor=None) -> FlujoDocumental:
        return FlujoDocumental(
            repositorio=repositorio,
            archivos=gestor_archivos,
            extractor=extractor,
            rpa=None,
            sincronizador_sheets=None,
            configuracion=configuracion,
            redactor=redactor,
        )
    return _crear


class TestRespaldoHeuristico:
    @pytest.mark.parametrize("codigo", [
        "AI_NO_CONFIGURADA", "AI_SERVICIO_NO_DISPONIBLE", "AI_CLIENTE_INVALIDO",
        "JSON_MALFORMADO", "SCHEMA_INVALIDO", "DOCUMENTO_ILEGIBLE_O_VACIO",
    ])
    def test_falla_de_ia_activa_la_heuristica_y_llega_a_revision(self, flujo, codigo):
        """
        Documento con texto reconocible: si la IA falla por cualquier causa
        "de disponibilidad/contrato" (no un bloqueo de seguridad), el
        pipeline debe rescatar folio/fecha por regex y dejar el documento
        en PENDIENTE_REVISION — nunca perderlo en la cuarentena de errores.
        """
        pipeline = flujo(_ExtractorFalso(codigo))
        registro = pipeline.ingestar_y_procesar("oficio.pdf", OrigenIngesta.WEB_DRAG_DROP, _pdf_con_oficio())

        assert registro.estado == EstadoDocumento.PENDIENTE_REVISION
        assert registro.extraccion_metodo == MetodoExtraccion.HEURISTICA_FALLBACK
        assert registro.numero_oficio == "DSA-2026-777-OF"
        assert registro.metadatos_extraidos.fecha_emision == "2026-08-15"

    def test_contenido_bloqueado_no_intenta_heuristica(self, flujo):
        """Un bloqueo de seguridad del proveedor NO se sortea: va directo a DESCARTADO."""
        pipeline = flujo(_ExtractorFalso("CONTENIDO_BLOQUEADO_SEGURIDAD"))
        registro = pipeline.ingestar_y_procesar("oficio.pdf", OrigenIngesta.WEB_DRAG_DROP, _pdf_con_oficio())

        assert registro.estado == EstadoDocumento.DESCARTADO
        assert "CONTENIDO_BLOQUEADO_SEGURIDAD" in registro.error_msg

    def test_sin_texto_disponible_igual_llega_a_revision_con_placeholders(self, flujo):
        """
        Documento sin capa de texto ni OCR (fax/escaneo puro, Tesseract no
        instalado): la heurística no encuentra nada, pero AÚN ASÍ produce
        un MetadatosOficio con placeholders — el documento sigue llegando a
        PENDIENTE_REVISION en vez de perderse en 04_errores.
        """
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"))
        registro = pipeline.ingestar_y_procesar("oficio.pdf", OrigenIngesta.WEB_DRAG_DROP, _pdf_en_blanco())

        assert registro.estado == EstadoDocumento.PENDIENTE_REVISION
        assert registro.extraccion_metodo == MetodoExtraccion.HEURISTICA_FALLBACK
        assert registro.numero_oficio == "S/N"


class TestPreprocesamientoHeuristicoPreviaALaIA:
    """Fase previa (core.heuristic_extractor.extraer_pistas): debe ejecutarse
    ANTES de llamar a extraer_de_paginas y reenviarse como kwarg, incluso
    cuando la IA tiene éxito (no es exclusiva del camino de respaldo)."""

    def test_pistas_llegan_a_extraer_de_paginas_en_camino_exitoso(self, flujo):
        extractor = _ExtractorCapturaPistas()
        pipeline = flujo(extractor)
        registro = pipeline.ingestar_y_procesar(
            "oficio.pdf", OrigenIngesta.WEB_DRAG_DROP, _pdf_con_oficio()
        )

        assert registro.estado == EstadoDocumento.PENDIENTE_REVISION
        assert registro.extraccion_metodo == MetodoExtraccion.IA  # no es el camino de respaldo
        assert len(extractor.llamadas) == 1
        pistas = extractor.llamadas[0]["pistas_heuristicas"]
        assert pistas is not None
        assert pistas.numero_oficio == "DSA-2026-777-OF"
        assert pistas.fecha_emision == "2026-08-15"

    def test_sin_texto_disponible_pasa_pistas_vacias_sin_abortar(self, flujo):
        """Documento sin capa de texto (fax/escaneo): el preprocesamiento no
        encuentra nada, pero la ingesta sigue su curso normal con la IA."""
        extractor = _ExtractorCapturaPistas()
        pipeline = flujo(extractor)
        registro = pipeline.ingestar_y_procesar(
            "oficio.pdf", OrigenIngesta.WEB_DRAG_DROP, _pdf_en_blanco()
        )

        assert registro.estado == EstadoDocumento.PENDIENTE_REVISION
        assert registro.extraccion_metodo == MetodoExtraccion.IA
        pistas = extractor.llamadas[0]["pistas_heuristicas"]
        assert pistas is not None
        assert pistas.hay_pistas is False


class TestVerificarDuplicado:
    """Chequeo síncrono previo a encolar (ver ui.views_dashboard._manejar_carga):
    programar_ingesta() es fire-and-forget, así que este es el único punto
    donde la UI puede avisar de un duplicado ANTES de que se pierda en el
    hilo de fondo (ver docstring de FlujoDocumental.verificar_duplicado)."""

    def test_sin_duplicado_devuelve_none(self, flujo):
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"))
        assert pipeline.verificar_duplicado(_pdf_con_oficio()) is None

    def test_duplicado_devuelve_el_registro_existente(self, flujo):
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"))
        contenido = _pdf_con_oficio()
        original = pipeline.ingestar_y_procesar("oficio.pdf", OrigenIngesta.WEB_DRAG_DROP, contenido)

        encontrado = pipeline.verificar_duplicado(contenido)

        assert encontrado is not None
        assert encontrado.id == original.id
        assert encontrado.nombre_archivo_original == "oficio.pdf"

    def test_no_encola_ni_dispara_ingestar_y_procesar(self, flujo):
        """El chequeo debe ser de solo lectura: no debe crear ningún
        registro por sí mismo, sea o no duplicado."""
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"))
        pipeline.verificar_duplicado(_pdf_con_oficio())
        assert pipeline.repo.listar() == []


class TestConfirmarRegistroManual:
    """FlujoDocumental.confirmar_registro_manual — certifica a mano un
    ERROR_RPA cuando el operador vio en la ventana visible de la Intranet
    que el oficio SÍ quedó registrado (ver rpa/playwright_rpa.py::
    _registrar_manejador_dialogos), pero el detector automático de folio
    no llegó a capturarlo a tiempo. Distinto de 'Reintentar RPA': no vuelve
    a abrir el navegador ni reenvía el formulario (evita un duplicado)."""

    def _documento_error_rpa(self, repositorio) -> DocumentoRegistro:
        metadatos = MetadatosOficio(
            numero_oficio="DSA-2026-777-OF",
            fecha_emision="2026-08-15",
            procedencia=Procedencia.AJENA,
            dependencia_area="NO ESPECIFICADO",
            remitente_nombre="ALGUIEN",
            destinatario_nombre="ALGUIEN MAS",
            asunto="Asunto de prueba con longitud suficiente para el contrato.",
        )
        return repositorio.crear(DocumentoRegistro(
            id=str(uuid.uuid4()),
            nombre_archivo_original="oficio.pdf",
            ruta_archivo_actual="03_procesados/oficio.pdf",
            origen=OrigenIngesta.WEB_DRAG_DROP,
            estado=EstadoDocumento.ERROR_RPA,
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            metadatos_validados=metadatos,
            error_msg="FOLIO_CONFIRMACION_NO_ENCONTRADO :: tiempo de espera agotado",
        ))

    def test_certifica_folio_y_pasa_a_completado(self, flujo, repositorio):
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"))
        documento = self._documento_error_rpa(repositorio)

        actualizado = pipeline.confirmar_registro_manual(documento.id, "HCG-OP-2026-009821", "ana")

        assert actualizado.estado == EstadoDocumento.COMPLETADO
        assert actualizado.rpa is not None
        assert actualizado.rpa.folio_acuse == "HCG-OP-2026-009821"
        assert actualizado.rpa.exitoso is True
        assert actualizado.rpa.simulado is False
        # El error_msg de la columna se limpia: exitoso=True (ver guardar_resultado_rpa).
        assert repositorio.obtener(documento.id).estado == EstadoDocumento.COMPLETADO

    def test_recorta_espacios_del_folio(self, flujo, repositorio):
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"))
        documento = self._documento_error_rpa(repositorio)
        actualizado = pipeline.confirmar_registro_manual(documento.id, "  HCG-OP-2026-009821  ", "ana")
        assert actualizado.rpa.folio_acuse == "HCG-OP-2026-009821"

    def test_folio_vacio_es_invalido(self, flujo, repositorio):
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"))
        documento = self._documento_error_rpa(repositorio)
        with pytest.raises(ValueError):
            pipeline.confirmar_registro_manual(documento.id, "   ", "ana")
        # No debe haber mutado el documento ante folio inválido.
        assert repositorio.obtener(documento.id).estado == EstadoDocumento.ERROR_RPA

    def test_documento_inexistente_es_invalido(self, flujo):
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"))
        with pytest.raises(ValueError):
            pipeline.confirmar_registro_manual("no-existe", "HCG-OP-2026-1", "ana")

    def test_solo_aplica_sobre_documentos_en_error_rpa(self, flujo, repositorio):
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"))
        documento = self._documento_error_rpa(repositorio)
        pipeline.confirmar_registro_manual(documento.id, "HCG-OP-2026-1", "ana")  # -> COMPLETADO

        with pytest.raises(ValueError):
            pipeline.confirmar_registro_manual(documento.id, "HCG-OP-2026-2", "ana")

    def test_queda_registrado_en_la_auditoria(self, flujo, repositorio):
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"))
        documento = self._documento_error_rpa(repositorio)
        pipeline.confirmar_registro_manual(documento.id, "HCG-OP-2026-1", "ana")

        historial = repositorio.listar_auditoria(documento.id)
        assert historial[0].accion == AccionAuditoria.CONFIRMAR_REGISTRO_MANUAL
        assert historial[0].revisor_usuario_id == "ana"
        assert historial[0].campos_modificados["folio_acuse"]["nuevo"] == "HCG-OP-2026-1"


class _RedactorFalso:
    """Doble de RedactorRespuestas: devuelve un borrador fijo y registra los
    argumentos recibidos, sin red ni credenciales reales."""

    def __init__(self):
        self.llamadas: list[dict] = []

    def generar_borrador(self, oficio_origen, peticion):
        self.llamadas.append({"oficio_origen": oficio_origen, "peticion": peticion})
        return RespuestaOficio(
            destinatario_nombre=oficio_origen.remitente_nombre,
            asunto="Respuesta generada por el doble de prueba",
            cuerpo_respuesta="Cuerpo de prueba.",
            despedida="Sin otro particular por el momento, quedo de usted.",
        )


class TestAsistenteDeRespuestaConIA:
    """core.pipeline.FlujoDocumental.generar_borrador_respuesta /
    actualizar_borrador_respuesta / generar_documento_respuesta — segundo
    flujo HITL del sistema (ver core/ai_responder.py, core/doc_generator.py)."""

    def test_generar_borrador_requiere_documento_existente(self, flujo):
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"), _RedactorFalso())
        with pytest.raises(ValueError, match="no encontrado"):
            pipeline.generar_borrador_respuesta("id-inexistente", PeticionRespuesta())

    def test_generar_borrador_requiere_metadatos_del_oficio(self, flujo):
        """Un documento aún en INGESTADO/EN_PREPROCESO no tiene de dónde
        partir para redactar una contestación."""
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"), _RedactorFalso())
        registro = pipeline.repo.crear(
            DocumentoRegistro(
                id="doc-sin-metadatos",
                nombre_archivo_original="oficio.pdf",
                ruta_archivo_actual="01_entrada/oficio.pdf",
                origen=OrigenIngesta.WEB_DRAG_DROP,
                estado=EstadoDocumento.INGESTADO,
                sha256="e" * 64,
            )
        )
        with pytest.raises(ValueError, match="metadatos"):
            pipeline.generar_borrador_respuesta(registro.id, PeticionRespuesta())

    def test_generar_borrador_persiste_y_devuelve_borrador(self, flujo):
        redactor = _RedactorFalso()
        pipeline = flujo(_ExtractorCapturaPistas(), redactor)
        documento = pipeline.ingestar_y_procesar("oficio.pdf", OrigenIngesta.WEB_DRAG_DROP, _pdf_con_oficio())
        peticion = PeticionRespuesta(sentido=SentidoRespuesta.REQUERIMIENTO_INFO, firmante_nombre="ANA TORRES")

        registro = pipeline.generar_borrador_respuesta(documento.id, peticion)

        assert registro.documento_id == documento.id
        assert registro.estado == EstadoRespuesta.BORRADOR
        assert registro.sentido == SentidoRespuesta.REQUERIMIENTO_INFO
        assert registro.firmante_nombre == "ANA TORRES"
        assert len(redactor.llamadas) == 1
        assert redactor.llamadas[0]["oficio_origen"].numero_oficio == documento.metadatos_extraidos.numero_oficio
        # Persistido de verdad en SQLite, no solo en memoria.
        assert pipeline.repo.obtener_respuesta(registro.id) is not None

    def test_actualizar_borrador_edita_sin_aprobar(self, flujo):
        pipeline = flujo(_ExtractorCapturaPistas(), _RedactorFalso())
        documento = pipeline.ingestar_y_procesar("oficio.pdf", OrigenIngesta.WEB_DRAG_DROP, _pdf_con_oficio())
        registro = pipeline.generar_borrador_respuesta(documento.id, PeticionRespuesta())

        editado = pipeline.actualizar_borrador_respuesta(
            registro.id,
            RespuestaOficio(
                destinatario_nombre="OTRO DESTINATARIO",
                asunto="Asunto editado a mano por el revisor",
                cuerpo_respuesta="Cuerpo editado.",
                despedida="Atentamente.",
            ),
            version_esperada=registro.version,
        )

        assert editado.estado == EstadoRespuesta.BORRADOR
        assert editado.respuesta.destinatario_nombre == "OTRO DESTINATARIO"

    def test_generar_documento_aprueba_y_produce_docx_en_storage(self, flujo, gestor_archivos):
        pipeline = flujo(_ExtractorCapturaPistas(), _RedactorFalso())
        documento = pipeline.ingestar_y_procesar("oficio.pdf", OrigenIngesta.WEB_DRAG_DROP, _pdf_con_oficio())
        registro = pipeline.generar_borrador_respuesta(documento.id, PeticionRespuesta())

        aprobado = pipeline.generar_documento_respuesta(registro.id, "ana")

        assert aprobado.estado == EstadoRespuesta.APROBADA
        assert aprobado.ruta_docx is not None
        assert aprobado.ruta_docx.startswith("05_respuestas/")
        assert gestor_archivos.existe(aprobado.ruta_docx)

    def test_generar_documento_respuesta_inexistente_lanza(self, flujo):
        pipeline = flujo(_ExtractorFalso("AI_NO_CONFIGURADA"), _RedactorFalso())
        with pytest.raises(ValueError, match="no encontrada"):
            pipeline.generar_documento_respuesta("respuesta-inexistente", "ana")
