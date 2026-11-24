; ===========================================================================
;  Auto-Xtract installer  (Inno Setup 6.3 or newer)
;
;  Compile with build_release.ps1 at the repository root. That script builds
;  AutoXtract.exe first, then runs ISCC on this file with /DAppVersion=x.y.z.
;
;  Or by hand, from this folder:
;      iscc setup.iss
;  which reads the version out of ..\VERSION instead.
; ===========================================================================

#ifndef AppVersion
  #define AppVersion Trim(FileRead(FileOpen(AddBackslash(SourcePath) + "..\VERSION")))
#endif

#define AppName        "Auto-Xtract"
#define AppShortName   "AutoXtract"
#define AppExeName     "AutoXtract.exe"
#define AppPublisher   "waggynotfound"
#define AppURL         "https://github.com/waggynotfound/auto-xtract"
; Never change AppId between releases, or Windows will list every version as a
; separate program instead of upgrading in place. The doubled brace is Inno's
; escape for a literal "{", so the real AppId is {B4F1C6D2-...}.
#define AppId          "{{B4F1C6D2-7A3E-4E59-9C1B-2F5D8A0E6C31}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases

VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} setup
VersionInfoProductName={#AppName}
VersionInfoCopyright=Copyright (c) {#AppPublisher}

DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
DisableWelcomePage=no

; Admin install into Program Files by default; the command line and the
; "Install for me only" option on the dialog let a user install per-account
; into %LOCALAPPDATA%\Programs without UAC.
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog commandline

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

OutputDir=..\dist
OutputBaseFilename={#AppShortName}-{#AppVersion}-Setup
SetupIconFile=..\app_icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
AllowUNCPath=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "sevenzip";   Description: "Open the 7-Zip download page (adds .7z and .rar support)"; GroupDescription: "Optional:"; Check: not SevenZipFound

[Files]
Source: "..\dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md";          DestDir: "{app}"; Flags: ignoreversion
Source: "..\CHANGELOG.md";       DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";              Filename: "{app}\{#AppExeName}"; Comment: "Extract archives and delete them after success"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";        Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
Filename: "https://7-zip.org/"; Description: "Open the 7-Zip download page"; Flags: shellexec nowait postinstall skipifsilent; Tasks: sevenzip

[UninstallDelete]
; The app keeps no settings, logs or cache: uninstalling removes everything.

[Code]
{ Mirrors find_7zip() in the app: PATH is ignored here, the usual install
  folders are checked instead, which is where 7-Zip puts itself. }
function SevenZipFound: Boolean;
var
  Root: String;
begin
  Result := False;
  if FileExists(ExpandConstant('{commonpf}\7-Zip\7z.exe')) then
    Result := True
  else if FileExists(ExpandConstant('{commonpf32}\7-Zip\7z.exe')) then
    Result := True
  else if FileExists(ExpandConstant('{localappdata}\Programs\7-Zip\7z.exe')) then
    Result := True
  else if RegQueryStringValue(HKLM, 'SOFTWARE\7-Zip', 'Path', Root)
       or RegQueryStringValue(HKLM32, 'SOFTWARE\7-Zip', 'Path', Root) then
    Result := FileExists(AddBackslash(Root) + '7z.exe');
end;
