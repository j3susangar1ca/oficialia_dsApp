; ============================================================================
; packaging/oficialia.iss — Instalador Windows de Oficialía Digital DSA
; ============================================================================
; Compilar con Inno Setup 6 (https://jrsoftware.org/isdl.php):
;     ISCC.exe packaging\oficialia.iss
; (normalmente invocado por packaging\build_windows.ps1 o por el workflow de
; GitHub Actions .github\workflows\build-windows-installer.yml — nunca a
; mano por el usuario final, que solo descarga y ejecuta el .exe resultante)
;
; Requiere que packaging\build_windows.ps1 (pasos 1-5) ya haya generado
; dist\OficialiaDigitalDSA\ (bundle de PyInstaller) con dist\OficialiaDigitalDSA
; \pw-browsers\ (Chromium de Playwright) copiado dentro.
;
; Diseño:
;   - Instala en {autopf}\OficialiaDigitalDSA (Archivos de programa) — de
;     solo lectura para usuarios estándar, como corresponde al CÓDIGO.
;   - Los DATOS (BD SQLite, PDFs, .env) viven en
;     {commonappdata}\OficialiaDigitalDSA (%ProgramData%), con permisos de
;     escritura para usuarios estándar: la app corre sin privilegios de
;     administrador día a día, solo el INSTALADOR los requiere.
;   - El componente "RPA" (navegador Chromium, ~300 MB) es opcional: quien
;     solo va a usar el modo simulación/HITL puede omitirlo.
;   - Nada se borra de %ProgramData% al desinstalar (Inno Setup no toca
;     directorios con archivos que no instaló él mismo: la BD y los PDFs
;     institucionales quedan a salvo de una desinstalación accidental).
; ============================================================================

#define MyAppName "Oficialía Digital DSA"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Hospital Civil de Guadalajara — División de Servicios Administrativos"
#define MyAppExeName "OficialiaDigitalDSA.exe"
#define MyDistDir "..\dist\OficialiaDigitalDSA"
#define MyDataDirName "OficialiaDigitalDSA"

[Setup]
AppId={{D5724293-2990-4D78-9C11-1E406A1DE0B2}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppSupportURL=https://github.com/j3susangar1ca/oficialia_dsapp
DefaultDirName={autopf}\{#MyDataDirName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=OficialiaDigitalDSA-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
; Windows 10 = NT 10.0 — cubre Windows 10 y 11, no instala en versiones anteriores.
MinVersion=10.0
SetupLogging=yes

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Types]
Name: "completa"; Description: "Instalación completa (recomendada)"
Name: "minima"; Description: "Solo la aplicación, sin automatización RPA"
Name: "personalizada"; Description: "Personalizada"; Flags: iscustom

[Components]
Name: "app"; Description: "Oficialía Digital DSA"; Types: completa minima personalizada; Flags: fixed
Name: "rpa"; Description: "Automatización RPA — navegador Chromium (~300 MB, requerido solo para RPA_MODO=playwright)"; Types: completa personalizada

[Files]
; Aplicación (PyInstaller onedir) sin el navegador — ese va aparte, como
; componente opcional, para no obligar a descargarlo si no se usa RPA real.
Source: "{#MyDistDir}\*"; Excludes: "pw-browsers\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs; Components: app

; Navegador Chromium de Playwright (componente opcional "rpa").
Source: "{#MyDistDir}\pw-browsers\*"; DestDir: "{app}\pw-browsers"; \
    Flags: ignoreversion recursesubdirs createallsubdirs; Components: rpa

; Plantilla de configuración: se coloca directamente como ".env" en la
; carpeta de datos SOLO si no existe ya (instalación limpia o reinstalo
; sin tocar la config de una instalación previa). La app también la crea
; sola si por algún motivo faltara (ver config.py::_sembrar_env_inicial).
Source: "..\.env.example"; DestDir: "{commonappdata}\{#MyDataDirName}"; \
    DestName: ".env"; Flags: onlyifdoesntexist uninsneveruninstall; Components: app

[Dirs]
; Carpeta de datos por máquina (BD, PDFs, .env) — permisos de escritura
; para usuarios estándar, ya que la aplicación corre sin privilegios de
; administrador en el día a día (solo el instalador los requiere).
Name: "{commonappdata}\{#MyDataDirName}"; Permissions: users-modify

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{group}\Configuración (.env)"; Filename: "notepad.exe"; \
    Parameters: """{commonappdata}\{#MyDataDirName}\.env"""; \
    Comment: "Editar GEMINI_API_KEY, credenciales RPA y Google Sheets"
Name: "{group}\Carpeta de datos (PDFs, base de datos)"; Filename: "{commonappdata}\{#MyDataDirName}"
Name: "{group}\Desinstalar {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    WorkingDir: "{app}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el Escritorio"; \
    GroupDescription: "Accesos directos:"

[Run]
; Excepción de Firewall para el puerto local de la interfaz — evita el
; aviso de "Windows Defender Firewall ha bloqueado algunas características"
; en el primer arranque (la app no necesita salir a Internet salvo por
; Gemini/RPA/Sheets, que usan la salida saliente normal, sin regla especial).
Filename: "netsh"; Parameters: "advfirewall firewall add rule name=""Oficialía Digital DSA"" dir=in action=allow program=""{app}\{#MyAppExeName}"" enable=yes profile=private,domain"; \
    Flags: runhidden; StatusMsg: "Configurando el Firewall de Windows…"
Filename: "{app}\{#MyAppExeName}"; Description: "Iniciar {#MyAppName} ahora"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "netsh"; Parameters: "advfirewall firewall delete rule name=""Oficialía Digital DSA"""; \
    Flags: runhidden
