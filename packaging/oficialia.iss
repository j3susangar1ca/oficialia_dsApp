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
;     página se omite sola: no se vuelven a pedir — SALVO que el usuario
;     elija lo contrario en la página "Datos de una instalación anterior"
;     (ver PaginaModoInstalacion en [Code]), que solo aparece cuando ya hay
;     datos de una instalación previa en esta PC y deja elegir entre
;     conservarlos, borrar solo las credenciales (para volver a capturarlas)
;     o hacer una instalación limpia (borra también la base de datos y los
;     PDFs — pide confirmación explícita por lo destructivo de esa opción).
;   - AppId fijo entre versiones (no cambiar): así Inno Setup reconoce una
;     instalación anterior como ACTUALIZACIÓN en el mismo {app}, en vez de
;     instalar en paralelo. CloseApplications cierra la app anterior si
;     sigue corriendo (evita el "archivo en uso" al sobrescribir el .exe) y
;     [InstallDelete] limpia {app} por completo antes de copiar los
;     archivos nuevos, para que ningún .dll/.pyd de una compilación de
;     PyInstaller anterior quede mezclado con los de esta versión (la causa
;     típica de que una instalación "encima" de otra quede en conflicto).
;   - Si quien compila el instalador definió GEMINI_API_KEY en su entorno
;     (variable local o secreto de GitHub Actions — ver
;     build_windows.ps1), esa clave real ya viene escrita en el .env de
;     CADA instalación nueva desde el paso [Files] de más abajo, así que en
;     la práctica ninguna PC de usuario final necesita capturarla: el campo
;     del asistente puede dejarse tal cual. La plantilla del repositorio
;     (.env.example) NUNCA lleva la clave real — solo la copia temporal
;     generada durante el build.
; ============================================================================

#define MyAppName "Oficialía Digital DSA"
#define MyAppVersion "1.2.0"
#define MyAppPublisher "Hospital Civil de Guadalajara — División de Servicios Administrativos"
#define MyAppExeName "OficialiaDigitalDSA.exe"
#define MyDistDir "..\dist\OficialiaDigitalDSA"
#define MyDataDirName "OficialiaDigitalDSA"
; Clave real tomada de la variable de entorno GEMINI_API_KEY de la máquina
; que COMPILA el instalador (nunca del repositorio) — ver
; packaging\build_windows.ps1 y .github\workflows\build-windows-installer.yml.
; Vacía si nadie la definió: el asistente simplemente pedirá la clave como
; hasta ahora. Sirve solo para prellenar el campo del asistente (ver
; InitializeWizard); el valor real ya queda escrito en el .env desplegado
; por el paso [Files] de más abajo, que toma ..\.env.example directamente.
#define DefaultGeminiApiKey GetEnv("GEMINI_API_KEY")

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
; Si una versión anterior sigue corriendo (servidor NiceGUI + navegador
; abiertos), Windows Restart Manager la detecta y la cierra automáticamente
; antes de sobrescribir {#MyAppExeName} y sus DLL — sin esto, el instalador
; fallaba con "archivo en uso" al actualizar sobre una instalación previa
; activa. No se reinicia sola (RestartApplications=no): el usuario decide
; cuándo volver a abrirla desde [Run] al terminar el asistente.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Types]
Name: "completa"; Description: "Instalación completa (recomendada)"
Name: "minima"; Description: "Solo la aplicación, sin automatización RPA"
Name: "personalizada"; Description: "Personalizada"; Flags: iscustom

[Components]
Name: "app"; Description: "Oficialía Digital DSA"; Types: completa minima personalizada; Flags: fixed
Name: "rpa"; Description: "Automatización RPA — navegador Chromium (~300 MB, requerido solo para RPA_MODO=playwright)"; Types: completa personalizada

[InstallDelete]
; Limpia {app} por completo ANTES de copiar los archivos de esta versión
; (corre antes que [Files]). PyInstaller regenera "_internal\" en cada
; build con nombres/versión de DLL que pueden cambiar entre releases;
; [Files] con "ignoreversion" solo sobrescribe lo que trae esta versión,
; nunca borra lo que sobra de una anterior — así que sin este paso, una
; actualización podía dejar mezclados .dll/.pyd de dos compilaciones
; distintas de PyInstaller y la app fallaba al arrancar ("conflicto con la
; versión pasada instalada"). Nada de esto toca %ProgramData% (BD, PDFs,
; .env): esos datos viven fuera de {app} — ver [Dirs] más abajo.
Type: filesandordirs; Name: "{app}"

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
//
// Antes de esa página va PaginaModoInstalacion: SOLO aparece si esta PC ya
// tiene datos de una instalación anterior en {commonappdata} (BD, PDFs y/o
// .env) y deja elegir entre conservarlos, borrar únicamente las credenciales
// (para forzar que la página de arriba vuelva a pedirlas) o hacer una
// instalación limpia (borra también la base de datos y los PDFs ya
// procesados — irreversible, por eso pide confirmación aparte en
// NextButtonClick). La opción por defecto es siempre "conservar todo": un
// instalador de actualización nunca borra nada por sorpresa, es el usuario
// quien tiene que elegirlo a propósito.
// ============================================================================
var
  PaginaModoInstalacion: TInputOptionWizardPage;
  PaginaCredenciales: TInputQueryWizardPage;
  PaginaCuentaServicio: TInputFileWizardPage;

const
  IdxGeminiApiKey = 0;
  IdxRpaUsuario = 1;
  IdxRpaPassword = 2;

  IdxModoConservarTodo = 0;
  IdxModoSoloCredenciales = 1;
  IdxModoInstalacionLimpia = 2;

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
  PaginaModoInstalacion := CreateInputOptionPage(wpSelectComponents,
    'Datos de una instalación anterior',
    'Esta PC ya tiene datos guardados de Oficialía Digital DSA',
    'Se encontró una carpeta de datos previa en ' +
    ExpandConstant('{commonappdata}\{#MyDataDirName}') + '. Elija qué ' +
    'hacer con ella antes de continuar. Si no está seguro, deje la opción ' +
    'recomendada: no se pierde nada.',
    True, False);
  PaginaModoInstalacion.Add(
    'Conservar todo (recomendado) — no toca la base de datos ni los PDFs; ' +
    'reutiliza las credenciales ya capturadas sin volver a pedirlas');
  PaginaModoInstalacion.Add(
    'Solo credenciales — vuelve a pedir la Gemini API key y la contraseña ' +
    'de la Intranet; conserva intactas la base de datos y los PDFs');
  PaginaModoInstalacion.Add(
    'Instalación limpia — borra la base de datos, los PDFs y las ' +
    'credenciales de esta PC; empieza completamente de cero');
  PaginaModoInstalacion.SelectedValueIndex := IdxModoConservarTodo;

  PaginaCredenciales := CreateInputQueryPage(PaginaModoInstalacion.ID,
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
  // Instalación nueva sin .env todavía: si este instalador se compiló con
  // una GEMINI_API_KEY por defecto (ver DefaultGeminiApiKey arriba), se
  // prellena aquí solo para que el campo no se vea vacío — el valor real ya
  // quedó escrito en el .env desplegado por [Files] antes de llegar a esta
  // página. Dejar el campo tal cual (sin tocarlo) es suficiente: no vuelve a
  // pedirse nada en esta PC.
  if PaginaCredenciales.Values[IdxGeminiApiKey] = '' then
    PaginaCredenciales.Values[IdxGeminiApiKey] := '{#DefaultGeminiApiKey}';
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

// PaginaModoInstalacion solo tiene sentido si ya hay algo que conservar o
// borrar: en una PC sin instalación previa se omite sola (no hay nada que
// preguntar) y el flujo queda igual que antes de agregar esta página.
//
// Si esta PC ya tiene capturadas las credenciales esenciales (GEMINI_API_KEY
// y RPA_PASSWORD) — de una instalación o reinstalación anterior — se omiten
// las páginas de credenciales/cuenta de servicio: es el "no preguntar nunca
// más" de siempre. La excepción es que el usuario haya elegido en
// PaginaModoInstalacion "solo credenciales" o "instalación limpia": ahí
// NUNCA se omiten, sin importar lo que ya haya en el .env — es justo lo que
// esas dos opciones piden.
function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = PaginaModoInstalacion.ID then
  begin
    Result := not DirExists(ExpandConstant('{commonappdata}\{#MyDataDirName}'));
    Exit;
  end;
  if (PageID = PaginaCredenciales.ID) or (PageID = PaginaCuentaServicio.ID) then
    Result := (PaginaModoInstalacion.SelectedValueIndex = IdxModoConservarTodo) and
              (LeerValorEnv('GEMINI_API_KEY') <> '') and
              (LeerValorEnv('RPA_PASSWORD') <> '');
end;

// Confirmación extra al salir de PaginaModoInstalacion si el usuario eligió
// "instalación limpia": es la única opción irreversible (borra la base de
// datos y los PDFs ya procesados), así que además de estar descrita en la
// propia página, se pide confirmar una vez más antes de dejarlo avanzar.
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = PaginaModoInstalacion.ID) and
     (PaginaModoInstalacion.SelectedValueIndex = IdxModoInstalacionLimpia) then
  begin
    Result := MsgBox(
      'Esto borrará PERMANENTEMENTE la base de datos, todos los PDFs de ' +
      'oficios ya procesados y las credenciales guardadas en esta PC (' +
      ExpandConstant('{commonappdata}\{#MyDataDirName}') + ').' + #13#13 +
      '¿Confirma que quiere continuar con la instalación limpia?',
      mbConfirmation, MB_YESNO) = IDYES;
  end;
end;

// Al llegar a la página de credenciales tras elegir "solo credenciales" o
// "instalación limpia", los campos deben verse vacíos (no los valores viejos
// con los que InitializeWizard los prellenó al abrir el asistente, antes de
// que el usuario eligiera el modo) — si no, parecería que no se está
// pidiendo nada nuevo. GEMINI_API_KEY sigue prellenándose con el valor de
// fábrica del instalador (DefaultGeminiApiKey) si lo trae: ese no es un dato
// viejo de ESTA pc, es el que trae el instalador nuevo.
procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = PaginaCredenciales.ID) and
     (PaginaModoInstalacion.SelectedValueIndex <> IdxModoConservarTodo) then
  begin
    PaginaCredenciales.Values[IdxGeminiApiKey] := '{#DefaultGeminiApiKey}';
    PaginaCredenciales.Values[IdxRpaUsuario] := '2010226';
    PaginaCredenciales.Values[IdxRpaPassword] := '';
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  RutaJson: String;
  LineasJson: TStringList;
  I: Integer;
  JsonEnUnaLinea: String;
begin
  // ssInstall corre justo ANTES de que [Files]/[Dirs] copien/recreen nada —
  // el momento correcto para aplicar el modo elegido en
  // PaginaModoInstalacion sobre los datos de una instalación anterior. Los
  // recrea todo lo necesario [Dirs] (permisos incluidos) y [Files] (plantilla
  // .env "onlyifdoesntexist") que corren justo después de este paso.
  if CurStep = ssInstall then
  begin
    case PaginaModoInstalacion.SelectedValueIndex of
      IdxModoSoloCredenciales:
        // Borra solo el .env (credenciales) — la base de datos y storage/
        // viven en subcarpetas aparte y no se tocan. [Files] repone un .env
        // en blanco desde la plantilla; el bloque de abajo (ssPostInstall)
        // ya escribe ahí los valores nuevos capturados en el asistente.
        if FileExists(RutaEnvDesplegado()) then
          DeleteFile(RutaEnvDesplegado());
      IdxModoInstalacionLimpia:
        // Borra TODO {commonappdata}\OficialiaDigitalDSA: .env, base de
        // datos SQLite y los PDFs/evidencia de oficios ya procesados.
        // Irreversible — ya se confirmó aparte en NextButtonClick.
        DelTree(ExpandConstant('{commonappdata}\{#MyDataDirName}'), True, True, True);
    end;
  end;

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
