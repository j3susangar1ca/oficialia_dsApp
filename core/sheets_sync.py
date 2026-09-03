"""
SISTEMA OFICIALIA-DIGITAL-DSA (reconstrucción 100% Python)
==========================================================
core/sheets_sync.py — Sincronización externa con Google Sheets (opcional).

Publica cada documento COMPLETADO como fila del "Tablero de Control de
Términos" mediante una cuenta de servicio, respetando el layout de columnas
A:M del adaptador original (`GoogleSheetsExternalSyncAdapter.ts`):

    A: Fecha registro | B: ID documento   | C: Folio oficio  | D: Fecha emisión
    E: Procedencia    | F: Dependencia    | G: Remitente      | H: Asunto
    I: Plazo (días)   | J: Datos sensibles| K: Archivo canónico
    L: Folio acuse RPA| M: RPA exitoso

Modo stub local: sin `GOOGLE_SHEETS_SPREADSHEET_ID` o sin credenciales de
Google Cloud disponibles (`credentials.json` junto a los datos de la app,
`GOOGLE_APPLICATION_CREDENTIALS` o `GOOGLE_SERVICE_ACCOUNT_JSON`), cada fila
se refleja en `data/sheets_backup.csv` — comportamiento "honesto" heredado
del original: el pipeline principal NUNCA se bloquea por Sheets; el
documento queda COMPLETADO y el estado de sincronización se persiste.
"""

from __future__ import annotations

import csv
import json
import logging
from typing import Optional

from config import DATOS_DIR, Configuracion, get_settings
from core.models import DocumentoRegistro, EstadoSheets, ResultadoRpa, ahora_utc_iso

logger = logging.getLogger("oficialia.sheets")

#: Encabezados del tablero (fila 1 del layout A:M del original).
ENCABEZADOS_TABLERO = [
    "Fecha registro",
    "ID documento",
    "Folio oficio",
    "Fecha emisión",
    "Procedencia",
    "Dependencia/Área",
    "Remitente",
    "Asunto",
    "Plazo (días)",
    "Datos sensibles",
    "Archivo canónico",
    "Folio acuse RPA",
    "RPA exitoso",
]


class SincronizadorSheets:
    """Actualización de filas del tablero externo (Google o stub local)."""

    def __init__(self, configuracion: Optional[Configuracion] = None) -> None:
        self.config = configuracion or get_settings()

    # ------------------------------------------------------------------
    # API pública (contrato usado por el pipeline)
    # ------------------------------------------------------------------
    @property
    def modo(self) -> str:
        """'google' | 'stub_local' según configuración disponible."""
        if self.config.sheets_configurado and self._credenciales_disponibles:
            return "google"
        return "stub_local"

    def registrar_documento(
        self, documento: DocumentoRegistro, resultado_rpa: ResultadoRpa
    ) -> EstadoSheets:
        """
        Agrega la fila del documento al tablero.

        :returns: estado de sincronización para persistir en la BD.
        :raises Exception: ante fallo de la API de Google (el llamador la
            trata como no bloqueante, igual que el original).
        """
        if self.modo == "google":
            return self._registrar_en_google(documento, resultado_rpa)
        return self._registrar_en_stub_local(documento, resultado_rpa)

    # ------------------------------------------------------------------
    # Fila del tablero (layout A:M)
    # ------------------------------------------------------------------
    @staticmethod
    def _construir_fila(documento: DocumentoRegistro, resultado_rpa: ResultadoRpa) -> list[str]:
        metadatos = documento.metadatos_validados
        assert metadatos is not None
        return [
            ahora_utc_iso(),
            documento.id,
            metadatos.numero_oficio,
            metadatos.fecha_emision,
            metadatos.procedencia.value,
            metadatos.dependencia_area,
            metadatos.remitente_nombre,
            metadatos.asunto,
            "" if metadatos.plazo_dias is None else str(metadatos.plazo_dias),
            "SÍ" if metadatos.contiene_datos_sensibles else "NO",
            documento.nombre_archivo_canonico or "",
            resultado_rpa.folio_acuse or "",
            "SÍ" if resultado_rpa.exitoso else "NO",
        ]

    # ------------------------------------------------------------------
    # Google Sheets (Service Account → gspread)
    # ------------------------------------------------------------------
    @property
    def _credenciales_disponibles(self) -> bool:
        """JSON inline o GOOGLE_APPLICATION_CREDENTIALS (ADC estándar)."""
        return bool(self.config.google_service_account_json.strip()) or self._ruta_adc() is not None

    def _ruta_adc(self) -> Optional[str]:
        """
        Credenciales de Application Default (ADC): `GOOGLE_APPLICATION_CREDENTIALS`
        si está definida, o si no un archivo `credentials.json` colocado junto a
        los datos de la app (uso personal: evita capturar el JSON en el .env).
        """
        import os

        ruta_env = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if ruta_env:
            return ruta_env

        candidato = DATOS_DIR / "credentials.json"
        return str(candidato) if candidato.is_file() else None

    def _abrir_hoja(self):
        """Abre (y cachea) la conexión a la hoja destino."""
        if getattr(self, "_hoja_cache", None) is not None:
            return self._hoja_cache

        import gspread
        from google.oauth2.service_account import Credentials

        alcances = ["https://www.googleapis.com/auth/spreadsheets"]
        json_inline = self.config.google_service_account_json.strip()
        if json_inline:
            credenciales = Credentials.from_service_account_info(
                json.loads(json_inline), scopes=alcances
            )
        else:
            ruta = self._ruta_adc()
            assert ruta is not None
            credenciales = Credentials.from_service_account_filename(ruta, scopes=alcances)

        cliente = gspread.authorize(credenciales)
        libro = cliente.open_by_key(self.config.google_sheets_spreadsheet_id.strip())
        try:
            hoja = libro.worksheet(self.config.google_sheets_sheet_name.strip())
        except gspread.WorksheetNotFound:
            hoja = libro.sheet1
            logger.warning(
                "Pestaña '%s' no encontrada; se usa la primera hoja del libro",
                self.config.google_sheets_sheet_name,
            )
        self._hoja_cache = hoja
        return hoja

    def _registrar_en_google(
        self, documento: DocumentoRegistro, resultado_rpa: ResultadoRpa
    ) -> EstadoSheets:
        hoja = self._abrir_hoja()
        respuesta = hoja.append_row(self._construir_fila(documento, resultado_rpa), value_input_option="RAW")

        fila = self._parsear_fila(respuesta)
        logger.info("Fila %s sincronizada en Google Sheets (doc %s)", fila, documento.id)
        return EstadoSheets(sincronizado=True, modo="google", fila_index=fila, timestamp=ahora_utc_iso())

    @staticmethod
    def _parsear_fila(respuesta) -> Optional[int]:
        """Extrae el índice de fila del updatedRange de la respuesta."""
        try:
            rango = respuesta.get("updates", {}).get("updatedRange", "")  # ej. 'Hoja1!A12:M12'
            import re

            coincidencia = re.search(r"![A-Z]+(\d+)", rango)
            return int(coincidencia.group(1)) if coincidencia else None
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Stub local (sin credenciales): espejo CSV en data/
    # ------------------------------------------------------------------
    def _registrar_en_stub_local(
        self, documento: DocumentoRegistro, resultado_rpa: ResultadoRpa
    ) -> EstadoSheets:
        ruta_csv = self.config.database_path.parent / "sheets_backup.csv"
        ruta_csv.parent.mkdir(parents=True, exist_ok=True)
        existe = ruta_csv.exists()

        with open(ruta_csv, "a", newline="", encoding="utf-8") as handle:
            escritor = csv.writer(handle)
            if not existe:
                escritor.writerow(ENCABEZADOS_TABLERO)
            escritor.writerow(self._construir_fila(documento, resultado_rpa))

        with open(ruta_csv, "r", encoding="utf-8") as handle:
            numero_fila = sum(1 for _ in handle)

        logger.warning(
            "[SHEETS-STUB] Fila del documento %s reflejada en %s (sin credenciales Google)",
            documento.id,
            ruta_csv,
        )
        return EstadoSheets(
            sincronizado=True, modo="stub_local", fila_index=numero_fila, timestamp=ahora_utc_iso()
        )
