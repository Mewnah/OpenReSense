; Inno Setup 6 script for the OpenReSense installer. Built by scripts\build_release.ps1 -Installer, which passes
; /DAppVersion=<x.y.z> /DSourceDir=<the built portable folder> /DOutputDir=<dist>.
; Per-user install (no admin rights). User data stays in {app}\cache across upgrades; uninstall asks before deleting it.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef NumericVersion
  #define NumericVersion "0.0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\OpenReSense"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef MaxDirLength
  ; 259 (the longest Windows path) - 1 - the deepest file's path inside the app folder; the build computes it
  #define MaxDirLength "115"
#endif

[Setup]
; never change AppId: upgrades find the installed copy by it
AppId={{3F5C38CE-3922-41FF-AD2E-0822E626BF4C}
AppName=OpenReSense
AppVersion={#AppVersion}
AppVerName=OpenReSense {#AppVersion}
VersionInfoVersion={#NumericVersion}
AppPublisher=Mewnah
DefaultDirName={autopf}\OpenReSense
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=OpenReSense-{#AppVersion}-setup
SetupIconFile=OpenReSense.ico
UninstallDisplayIcon={app}\OpenReSense.exe
UninstallDisplayName=OpenReSense
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; a running copy (its window, and the hidden Python server Windows cannot ask to close) would otherwise make an
; upgrade abort
CloseApplications=force
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; an upgrade gets fresh program folders, so files an older version had never mix in (old packages break imports).
; Not catalog (catalog\user holds your own sources) and not cache (your data).
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\server"
Type: filesandordirs; Name: "{app}\web"
Type: filesandordirs; Name: "{app}\node_modules"
Type: filesandordirs; Name: "{app}\scripts"
Type: filesandordirs; Name: "{app}\licenses"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\OpenReSense"; Filename: "{app}\OpenReSense.exe"
Name: "{autodesktop}\OpenReSense"; Filename: "{app}\OpenReSense.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\OpenReSense.exe"; Description: "{cm:LaunchProgram,OpenReSense}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; compiled files Python writes next to the code at run time
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\server"
Type: filesandordirs; Name: "{app}\scripts"

[Code]
function NextButtonClick(CurPageID: Integer): Boolean;  // also runs for silent installs (/DIR=...)
var
  Msg: String;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and (Length(WizardDirValue) > {#MaxDirLength}) then
  begin
    Msg := 'This folder''s path is ' + IntToStr(Length(WizardDirValue)) + ' characters long. OpenReSense needs one of at ' +
      'most {#MaxDirLength} characters: Windows cannot create its deepest files otherwise. Please choose a shorter ' +
      'folder, such as' + #13#10 + ExpandConstant('{autopf}\OpenReSense');
    SuppressibleMsgBox(Msg, mbError, MB_OK, IDOK);
    Log(Msg);
    Result := False;
  end;
end;

procedure StopRunningApp();  // only copies started from this folder (not a portable copy elsewhere)
var
  Dir: String;
  Code: Integer;
begin
  Dir := ExpandConstant('{app}\');
  StringChangeEx(Dir, '''', '''''', True);  // a quote inside a PowerShell '...' string
  // not unins*.exe: the uninstaller's first stage runs from this folder until the uninstall ends; stopping it made the
  // uninstall report failure (exit code -1) although it had worked
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), '-NoProfile -NonInteractive -ExecutionPolicy Bypass ' +
    '-Command "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith(''' + Dir +
    ''', [StringComparison]::OrdinalIgnoreCase) -and $_.Name -notlike ''unins*'' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"',
    '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data, Web: String;
  I: Integer;
begin
  if CurUninstallStep = usUninstall then
    StopRunningApp();  // a running copy keeps its files locked and its port busy
  if CurUninstallStep = usPostUninstall then
  begin
    Data := ExpandConstant('{app}\cache');
    Web := ExpandConstant('{localappdata}\OpenReSense');  // the app window's browser data, size and layout
    // (a line must not start with #13#10: the preprocessor reads lines starting with # as its own)
    if (DirExists(Data) or DirExists(Web)) and (SuppressibleMsgBox('Also delete your OpenReSense data (downloaded files, dataset list, settings and the window''s browser data) in' + #13#10 +
        Data + #13#10 + Web + ' ?', mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES) then
    begin
      DelTree(Data, True, True, True);
      for I := 1 to 10 do  // the window's browser engine may still be closing for a moment
      begin
        if not DirExists(Web) then Break;
        if not DelTree(Web, True, True, True) then Sleep(500);
      end;
      if DirExists(Web) then
        SuppressibleMsgBox('Some files in ' + Web + ' are in use (is OpenReSense running from another folder?) and were not deleted.',
          mbInformation, MB_OK, IDOK);
    end;
  end;
end;
