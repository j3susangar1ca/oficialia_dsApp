<#
.SYNOPSIS
    Construye el ejecutable de Oficialía Digital DSA y el instalador final
    (.exe) para Windows 10/11.

.DESCRIPTION
    Este script corre en la máquina que CONSTRUYE el instalador (un
    desarrollador/IT de la DSA, o el runner de GitHub Actions en
    .github\workflows\build-windows-installer.yml). El usuario final que
    descarga "OficialiaDigitalDSA-Setup.exe" desde GitHub Releases NO
    ejecuta este script ni necesita Python, pip, Playwright ni ninguna otra
    dependencia: todo eso queda embebido en el instalador por este proceso.

    Pasos:
      1. Crea un entorno virtual limpio en build\venv
      2. Instala requirements.txt + dependencias de compilación (PyInstaller)
      3. Descarga el navegador Chromium de Playwright a build\pw-browsers
         (para que RPA_MODO=playwright funcione sin `playwright install`)
      4. Construye el bundle "onedir" con PyInstaller (packaging\oficialia.spec)
      5. Copia pw-browsers junto al ejecutable construido
      6. Compila el instalador final con Inno Setup (ISCC.exe), si está disponible

    GEMINI_API_KEY por defecto (para que ninguna PC de usuario final tenga
    que capturarla a mano):
    Si esta máquina de build tiene la variable de entorno GEMINI_API_KEY
    definida (localmente por el desarrollador, o como secreto de GitHub
    Actions — ver .github\workflows\build-windows-installer.yml), el
    script sustituye TEMPORALMENTE esa clave en la copia de trabajo de
    .env.example antes de empaquetar (pasos 4 y 6, que la leen), y la
    restaura al terminar — nunca se guarda en git. Con eso, toda
    instalación NUEVA queda con GEMINI_API_KEY ya funcional desde el
    primer arranque (ver [Files] en packaging\oficialia.iss y
    config.py::_sembrar_env_inicial). Las PC que ya tenían un .env con la
    clave vacía se auto-reparan solas al actualizar a un instalador
    construido así (ver config.py::_reparar_gemini_api_key_faltante) — no
    hace falta editar ningún .env/.txt a mano en ninguna máquina.
    Si la variable no está definida, el instalador se genera igual que
    antes (el asistente pedirá la clave la primera vez, como siempre).

.PARAMETER SinInstalador
    Omite el paso 6 — genera solo dist\OficialiaDigitalDSA para pruebas
    locales rápidas, sin requerir Inno Setup instalado.

.EXAMPLE
    .\packaging\build_windows.ps1
    Construye todo: bundle + instalador final en dist\installer\.

.EXAMPLE
    $env:GEMINI_API_KEY = "AIza..."
    .\packaging\build_windows.ps1
    Igual que arriba, pero con la clave real ya incrustada por defecto en
    el instalador resultante (solo para este build, nunca queda en disco
    del repositorio ni se commitea).

.EXAMPLE
    .\packaging\build_windows.ps1 -SinInstalador
    Solo genera dist\OficialiaDigitalDSA (para probar el .exe directamente).
#>
[CmdletBinding()]
param(
    [switch]$SinInstalador
)

$ErrorActionPreference = "Stop"
$Raiz = Split-Path -Parent $PSScriptRoot
Set-Location $Raiz

function Escribir-Paso($Texto) {
    Write-Host ""
    Write-Host "== $Texto ==" -ForegroundColor Cyan
}

# ----------------------------------------------------------------------
# 1) Entorno virtual de construcción
# ----------------------------------------------------------------------
Escribir-Paso "1/6 Entorno virtual de construcción"
if (-not (Test-Path "build\venv\Scripts\python.exe")) {
    python -m venv build\venv
}
$Python = Join-Path $Raiz "build\venv\Scripts\python.exe"
$Pip = Join-Path $Raiz "build\venv\Scripts\pip.exe"

# ----------------------------------------------------------------------
# 2) Dependencias (requirements.txt + PyInstaller)
# ----------------------------------------------------------------------
Escribir-Paso "2/6 Instalando dependencias (requirements.txt + PyInstaller)"
& $Python -m pip install --upgrade pip | Out-Null
& $Pip install -r requirements.txt
& $Pip install -r packaging\requirements-build.txt

# ----------------------------------------------------------------------
# 3) Navegador Chromium de Playwright (vendorizado, no el caché global)
# ----------------------------------------------------------------------
Escribir-Paso "3/6 Descargando navegador Chromium de Playwright"
New-Item -ItemType Directory -Path "build" -Force | Out-Null
$RutaBrowsers = Join-Path $Raiz "build\pw-browsers"
$env:PLAYWRIGHT_BROWSERS_PATH = $RutaBrowsers
& $Python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    throw "Falló 'playwright install chromium'. Revise su conexión a Internet."
}

# ----------------------------------------------------------------------
# 3.5) Incrustar GEMINI_API_KEY por defecto — SOLO en esta copia de
#      trabajo y solo mientras dura el build (ver .PARAMETER arriba).
# ----------------------------------------------------------------------
$RutaEnvExample = Join-Path $Raiz ".env.example"
$ContenidoOriginalEnvExample = Get-Content $RutaEnvExample -Raw
$ClaveIncrustada = $false

function Restaurar-PlantillaEnv {
    if ($script:ClaveIncrustada) {
        Set-Content -Path $script:RutaEnvExample -Value $script:ContenidoOriginalEnvExample -NoNewline
        $script:ClaveIncrustada = $false
    }
}

if ($env:GEMINI_API_KEY) {
    Escribir-Paso "3.5/6 Incrustando GEMINI_API_KEY por defecto (temporal, no se commitea)"
    $NuevoContenido = $ContenidoOriginalEnvExample -replace '(?m)^GEMINI_API_KEY=.*$', "GEMINI_API_KEY=$($env:GEMINI_API_KEY)"
    Set-Content -Path $RutaEnvExample -Value $NuevoContenido -NoNewline
    $ClaveIncrustada = $true
} else {
    Write-Warning "GEMINI_API_KEY no está definida en el entorno de build: el instalador quedará SIN clave por defecto (cada PC la pedirá una vez, como hasta ahora). Defínala antes de compilar para que quede preconfigurada de fábrica."
}

try {
    # ----------------------------------------------------------------------
    # 4) Empaquetado con PyInstaller (onedir)
    # ----------------------------------------------------------------------
    Escribir-Paso "4/6 Empaquetando la aplicación con PyInstaller"
    Remove-Item -Recurse -Force "dist\OficialiaDigitalDSA" -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force "build\pyinstaller" -ErrorAction SilentlyContinue
    & $Python -m PyInstaller packaging\oficialia.spec --noconfirm --distpath dist --workpath build\pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller falló. Revise el log anterior (suele ser un import faltante: agréguelo a PAQUETES_COMPLETOS en packaging\oficialia.spec)."
    }

    # ----------------------------------------------------------------------
    # 5) Copiar el navegador Chromium junto al ejecutable
    # ----------------------------------------------------------------------
    Escribir-Paso "5/6 Copiando el navegador Chromium al bundle"
    Copy-Item -Recurse -Force $RutaBrowsers "dist\OficialiaDigitalDSA\pw-browsers"

    if ($SinInstalador) {
        Write-Host ""
        Write-Host "Listo (sin instalador): dist\OficialiaDigitalDSA\OficialiaDigitalDSA.exe" -ForegroundColor Green
        Restaurar-PlantillaEnv
        exit 0
    }

    # ----------------------------------------------------------------------
    # 6) Instalador final con Inno Setup
    # ----------------------------------------------------------------------
    Escribir-Paso "6/6 Compilando el instalador con Inno Setup"
    $Iscc = Get-Command "iscc.exe" -ErrorAction SilentlyContinue
    if (-not $Iscc) {
        $RutasComunes = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
        )
        foreach ($ruta in $RutasComunes) {
            if (Test-Path $ruta) { $Iscc = Get-Item $ruta; break }
        }
    }
    if (-not $Iscc) {
        Write-Warning "Inno Setup (ISCC.exe) no encontrado en PATH."
        Write-Warning "Instálelo desde https://jrsoftware.org/isdl.php, o vía Chocolatey: choco install innosetup"
        Write-Warning "También puede ejecutar este script con -SinInstalador para solo generar dist\OficialiaDigitalDSA"
        Restaurar-PlantillaEnv
        exit 1
    }

    $RutaIscc = if ($Iscc -is [System.Management.Automation.CommandInfo]) { $Iscc.Source } else { $Iscc.FullName }
    & $RutaIscc "packaging\oficialia.iss"
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup (ISCC.exe) falló al compilar packaging\oficialia.iss"
    }

    Write-Host ""
    Write-Host "Instalador generado: dist\installer\OficialiaDigitalDSA-Setup.exe" -ForegroundColor Green
} finally {
    # Se ejecuta también ante error (throw) — la plantilla del repositorio
    # nunca debe quedar en disco con la clave real puesta.
    Restaurar-PlantillaEnv
}
