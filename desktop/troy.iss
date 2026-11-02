; Установщик программы «Трой» (Inno Setup 6). Собирается из build.bat.

#define AppName "Troy"
#ifndef AppVersion
#define AppVersion "0.5.3"
#endif
#define AppExe "SomeTroy.exe"

[Setup]
AppId={{514A1243-2F8B-4AF6-9F0D-6A63DC345DDA}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Claude и Egor Zhidetskiy
AppCopyright=© 2026 Claude и Egor Zhidetskiy
DefaultDirName={localappdata}\Programs\SomeTroy
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=.
OutputBaseFilename=SomeTroySetup-{#AppVersion}
SetupIconFile=troy.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} — личный ассистент Трой
WizardStyle=modern
VersionInfoVersion={#AppVersion}
VersionInfoCompany=Claude и Egor Zhidetskiy
VersionInfoCopyright=© 2026 Claude и Egor Zhidetskiy
VersionInfoDescription=Установщик Троя
Compression=lzma2
SolidCompression=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "Ярлык на рабочем столе"; GroupDescription: "Дополнительно:"
Name: "defender"; Description: "Добавить Троя в исключения Защитника Windows (попросит права администратора)"; GroupDescription: "Защитник Windows:"
Name: "autostart"; Description: "Запускать вместе с Windows (свёрнутым в трей)"; GroupDescription: "Дополнительно:"; Flags: unchecked

[InstallDelete]
; ярлыки со старым названием «Трой» — убираем, новые создадутся с названием Troy
Type: files; Name: "{autoprograms}\Трой.lnk"
Type: files; Name: "{autodesktop}\Трой.lnk"
Type: files; Name: "{userstartup}\Трой.lnk"
Type: files; Name: "{autoprograms}\Трой — обновить и починить сервер.lnk"
Type: files; Name: "{autoprograms}\Трой — починить связь с сервером.lnk"
; после каждой установки — снова вход по паролю
Type: files; Name: "{localappdata}\SomeTroy\session.json"

[Code]
const
  RegKey = 'Software\SomeTroy';

procedure KillTroy();
var Code: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM SomeTroy.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

{ Команда Защитника с правами администратора (Windows спросит разрешение) }
function DefenderCmd(const Cmd, Path: String): Boolean;
var Code: Integer;
begin
  Result := ShellExec('runas', 'powershell.exe',
    '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "' + Cmd + ' -ExclusionPath ''' + Path + '''"',
    '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var AppPath: String;
begin
  KillTroy();
  Result := '';
  { исключение — ДО копирования файлов, иначе Защитник может удалить программу прямо при установке }
  if WizardIsTaskSelected('defender') then
  begin
    AppPath := ExpandConstant('{app}');
    if DefenderCmd('Add-MpPreference', AppPath) then
      RegWriteStringValue(HKEY_CURRENT_USER, RegKey, 'DefenderExclusion', AppPath)
    else
      MsgBox('Не удалось добавить исключение в Защитник Windows (нет разрешения администратора).' + #13#10 +
             'Установка продолжится. Если Защитник удалит Троя — добавь папку' + #13#10 + AppPath + #13#10 +
             'в исключения вручную.', mbInformation, MB_OK);
  end;
end;

function InitializeUninstall(): Boolean;
begin
  KillTroy();
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var AppPath: String;
begin
  if (CurUninstallStep = usPostUninstall) and
     RegQueryStringValue(HKEY_CURRENT_USER, RegKey, 'DefenderExclusion', AppPath) then
  begin
    DefenderCmd('Remove-MpPreference', AppPath);
    RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER, RegKey);
  end;
end;

[Files]
Source: "dist\SomeTroy\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "config.json"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "troy-cert.pem"; DestDir: "{app}"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\tools\fix-connection.bat"; DestDir: "{app}\tools"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{autoprograms}\{#AppName} — обновить и починить сервер"; Filename: "{app}\tools\fix-connection.bat"; WorkingDir: "{app}\tools"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "--tray"; WorkingDir: "{app}"; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "Открыть Троя"; Flags: nowait postinstall skipifsilent
; обновление из самой программы ставится тихо — после него Трой запускается сам
Filename: "{app}\{#AppExe}"; Flags: nowait skipifnotsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
; окно программы хранит здесь свои настройки (разрешение микрофона и т.п.)
Type: filesandordirs; Name: "{localappdata}\SomeTroy"
