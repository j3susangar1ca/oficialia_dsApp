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
;   - El asistente captura las credenciales (GEMINI_API_KEY, usuario y
;     contraseña de la Intranet, cuenta de servicio de Google opcional) en
;     una página propia y las escribe directamente en el .env desplegado
;     (ver sección [Code]) — nadie tiene que abrir Notepad a mano. Si el
;     .env ya trae esas claves capturadas (reinstalación/actualización), la
;     página se omite sola: no se vuelven a pedir.
; ============================================================================

#define MyAppName "Oficialía Digital DSA"
#define MyAppVersion "1.1.0"
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
Name: "{group}\Configuración avanzada (.env)"; Filename: "notepad.exe"; \
    Parameters: """{commonappdata}\{#MyDataDirName}\.env"""; \
    Comment: "Ajustes avanzados (SMB, hoja de Sheets, timeouts…) — las credenciales principales ya se capturaron durante la instalación"
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

[Code]
// ============================================================================
// Página propia del asistente: captura las credenciales de esta instalación
// (GEMINI_API_KEY, usuario/contraseña de la Intranet y, opcionalmente, la
// cuenta de servicio de Google) y las escribe directamente en el .env
// desplegado en {commonappdata}\OficialiaDigitalDSA\.env — nadie tiene que
// abrir Notepad a mano. Si esas claves YA están capturadas (reinstalación o
// actualización sobre una PC ya configurada), la página se omite sola: ver
// ShouldSkipPage más abajo. Todo lo demás del .env (SMB, Sheets, timeouts…)
// sigue siendo editable a mano desde el acceso directo "Configuración
// avanzada (.env)" del menú Inicio.
// ============================================================================
var
  PaginaCredenciales: TInputQueryWizardPage;
  PaginaCuentaServicio: TInputFileWizardPage;

const
  IdxGeminiApiKey = 0;
  IdxRpaUsuario = 1;
  IdxRpaPassword = 2;

// Ruta del .env ya desplegado (o donde quedará tras [Files]) en esta PC.
function RutaEnvDesplegado(): String;
begin
  Result := ExpandConstant('{commonappdata}\{#MyDataDirName}\.env');
end;

// Valor actual de CLAVE= dentro del .env desplegado, o '' si el archivo no
// existe todavía o la clave no aparece (incluida comentada con '#'). Se usa
// tanto para prellenar el asistente en una reinstalación como para decidir
// si ya no hace falta volver a pedir las credenciales (ShouldSkipPage).
function LeerValorEnv(const Clave: String): String;
var
  Lineas: TStringList;
  I: Integer;
  Linea, Prefijo: String;
begin
  Result := '';
  if not FileExists(RutaEnvDesplegado()) then
    Exit;
  Lineas := TStringList.Create;
  try
    Lineas.LoadFromFile(RutaEnvDesplegado());
    Prefijo := Clave + '=';
    for I := 0 to Lineas.Count - 1 do
    begin
      Linea := Trim(Lineas[I]);
      if Copy(Linea, 1, Length(Prefijo)) = Prefijo then
      begin
        Result := Copy(Linea, Length(Prefijo) + 1, MaxInt);
        Break;
      end;
    end;
  finally
    Lineas.Free;
  end;
end;

// Reemplaza (o agrega si no existía, comentada o no) la línea "CLAVE=VALOR"
// dentro del .env desplegado, sin tocar ninguna otra línea. Nunca se llama
// con Valor vacío (ver CurStepChanged) para no borrar por accidente un
// valor ya capturado en una instalación anterior.
procedure EscribirValorEnv(const Clave, Valor: String);
var
  Lineas: TStringList;
  I: Integer;
  Linea, Prefijo, PrefijoComentado: String;
  Encontrada: Boolean;
begin
  Lineas := TStringList.Create;
  try
    if FileExists(RutaEnvDesplegado()) then
      Lineas.LoadFromFile(RutaEnvDesplegado());
    Prefijo := Clave + '=';
    PrefijoComentado := '#' + Prefijo;
    Encontrada := False;
    for I := 0 to Lineas.Count - 1 do
    begin
      Linea := Trim(Lineas[I]);
      if (Copy(Linea, 1, Length(Prefijo)) = Prefijo) or
         (Copy(Linea, 1, Length(PrefijoComentado)) = PrefijoComentado) then
      begin
        Lineas[I] := Clave + '=' + Valor;
        Encontrada := True;
        Break;
      end;
    end;
    if not Encontrada then
      Lineas.Add(Clave + '=' + Valor);
    ForceDirectories(ExtractFileDir(RutaEnvDesplegado()));
    Lineas.SaveToFile(RutaEnvDesplegado());
  finally
    Lineas.Free;
  end;
end;

procedure InitializeWizard();
begin
  PaginaCredenciales := CreateInputQueryPage(wpSelectComponents,
    'Credenciales de esta instalación',
    'Se capturan una sola vez — no se le volverán a pedir',
    'Estos valores se guardan directamente en el .env de esta PC (' +
    RutaEnvDesplegado() + '). Puede dejar en blanco lo que no use por ' +
    'ahora y completarlo después desde "Configuración avanzada (.env)" ' +
    'en el menú Inicio.');
  PaginaCredenciales.Add('Gemini API key (extracción con IA):', False);
  PaginaCredenciales.Add('Usuario de la Intranet (RPA_USUARIO):', False);
  PaginaCredenciales.Add('Contraseña de la Intranet (RPA_PASSWORD):', True);
  PaginaCredenciales.Values[IdxGeminiApiKey] := LeerValorEnv('GEMINI_API_KEY');
  PaginaCredenciales.Values[IdxRpaUsuario] := LeerValorEnv('RPA_USUARIO');
  if PaginaCredenciales.Values[IdxRpaUsuario] = '' then
    PaginaCredenciales.Values[IdxRpaUsuario] := '2010226';
  PaginaCredenciales.Values[IdxRpaPassword] := LeerValorEnv('RPA_PASSWORD');

  PaginaCuentaServicio := CreateInputFilePage(PaginaCredenciales.ID,
    'Google Sheets (opcional)',
    'Cuenta de servicio para el Tablero de Control',
    'Si no cuenta con una todavía, deje esto en blanco: la app sigue ' +
    'funcionando (las filas se respaldan en un CSV local) hasta que la ' +
    'agregue después. Seleccione el archivo .json de la cuenta de ' +
    'servicio de Google descargado desde Google Cloud Console.');
  PaginaCuentaServicio.Add('Archivo .json de la cuenta de servicio:',
    'Archivos JSON (*.json)|*.json|Todos los archivos (*.*)|*.*', '.json');
end;

// Si esta PC ya tiene capturadas las credenciales esenciales (GEMINI_API_KEY
// y RPA_PASSWORD) — de una instalación o reinstalación anterior — se omiten
// ambas páginas: es exactamente el "no preguntar nunca más".
function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if (PageID = PaginaCredenciales.ID) or (PageID = PaginaCuentaServicio.ID) then
    Result := (LeerValorEnv('GEMINI_API_KEY') <> '') and
              (LeerValorEnv('RPA_PASSWORD') <> '');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  RutaJson: String;
  LineasJson: TStringList;
  I: Integer;
  JsonEnUnaLinea: String;
begin
  if CurStep <> ssPostInstall then
    Exit;

  if PaginaCredenciales.Values[IdxGeminiApiKey] <> '' then
    EscribirValorEnv('GEMINI_API_KEY', PaginaCredenciales.Values[IdxGeminiApiKey]);
  if PaginaCredenciales.Values[IdxRpaUsuario] <> '' then
    EscribirValorEnv('RPA_USUARIO', PaginaCredenciales.Values[IdxRpaUsuario]);
  if PaginaCredenciales.Values[IdxRpaPassword] <> '' then
    EscribirValorEnv('RPA_PASSWORD', PaginaCredenciales.Values[IdxRpaPassword]);

  // El .json de la cuenta de servicio se aplana a una sola línea (el campo
  // GOOGLE_SERVICE_ACCOUNT_JSON del .env es de una sola línea); las claves
  // internas del JSON (p. ej. private_key) ya traen sus saltos de línea
  // escapados como "\n" dentro de la cadena, así que unir las líneas del
  // archivo sin separador no corrompe el JSON.
  RutaJson := PaginaCuentaServicio.Values[0];
  if (RutaJson <> '') and FileExists(RutaJson) then
  begin
    LineasJson := TStringList.Create;
    try
      LineasJson.LoadFromFile(RutaJson);
      JsonEnUnaLinea := '';
      for I := 0 to LineasJson.Count - 1 do
        JsonEnUnaLinea := JsonEnUnaLinea + Trim(LineasJson[I]);
      if JsonEnUnaLinea <> '' then
        EscribirValorEnv('GOOGLE_SERVICE_ACCOUNT_JSON', JsonEnUnaLinea);
    finally
      LineasJson.Free;
    end;
  end;
end;
