; Inno Setup 6 installer script for Omnimex
; Compiles dist\Omnimex\* into dist\installer\Omnimex-Setup-<version>.exe

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#define AppName "Omnimex"
#define AppPublisher "Omnimex"
#define AppExeName "Omnimex.exe"
#define AppCliName "omnimex-cli.exe"
#define AppURL "https://github.com/omnimex/omnimex"

[Setup]
AppId={{9B5C2E3F-6A8B-4D17-9F42-5C8B3E2A1901}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist\installer
OutputBaseFilename=Omnimex-Setup-{#AppVersion}
SetupIconFile=..\data\omnimex.ico
UninstallDisplayIcon={app}\{#AppExeName}
Compression=lzma2/normal
SolidCompression=yes
WizardStyle=modern
ChangesEnvironment=yes
CloseApplications=yes
RestartApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "addtopath"; Description: "Add omnimex-cli to user PATH"; GroupDescription: "Developer Integration"
Name: "startupicon"; Description: "Launch Omnimex automatically when signing in to Windows"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\Omnimex\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{group}\{#AppName} CLI"; Filename: "{sys}\cmd.exe"; Parameters: "/K """"{app}\{#AppCliName}"" --help"""; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Omnimex"; ValueData: """{app}\{#AppExeName}"""; Flags: uninsdeletevalue; Tasks: startupicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  EnvironmentKey = 'Environment';

function NormalizeDir(const Value: string): string;
begin
  Result := Lowercase(Trim(Value));
  while (Length(Result) > 0) and (Result[Length(Result)] = '\') do
    Delete(Result, Length(Result), 1);
end;

procedure AddDirToUserPath(const DirPath: string);
var
  CurrentPath: string;
  Needle: string;
  Haystack: string;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', CurrentPath) then
    CurrentPath := '';

  Needle := ';' + NormalizeDir(DirPath) + ';';
  Haystack := ';' + Lowercase(CurrentPath) + ';';
  if Pos(Needle, Haystack) > 0 then
    Exit;

  if (CurrentPath <> '') and (CurrentPath[Length(CurrentPath)] <> ';') then
    CurrentPath := CurrentPath + ';';
  CurrentPath := CurrentPath + DirPath;

  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', CurrentPath);
end;

procedure RemoveDirFromUserPath(const DirPath: string);
var
  CurrentPath: string;
  Target: string;
  Segment: string;
  Remaining: string;
  Updated: string;
  SepPos: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', CurrentPath) then
    Exit;

  Target := NormalizeDir(DirPath);
  Remaining := CurrentPath;
  Updated := '';

  while Remaining <> '' do
  begin
    SepPos := Pos(';', Remaining);
    if SepPos > 0 then
    begin
      Segment := Copy(Remaining, 1, SepPos - 1);
      Delete(Remaining, 1, SepPos);
    end
    else
    begin
      Segment := Remaining;
      Remaining := '';
    end;

    if (Trim(Segment) <> '') and (NormalizeDir(Segment) <> Target) then
    begin
      if Updated <> '' then
        Updated := Updated + ';';
      Updated := Updated + Segment;
    end;
  end;

  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Updated);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddDirToUserPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveDirFromUserPath(ExpandConstant('{app}'));
end;
