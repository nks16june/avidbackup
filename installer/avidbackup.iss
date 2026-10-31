; Inno Setup script for AvidBackup (Windows)
; Build after PyInstaller: ISCC installer\avidbackup.iss
; Defines (override from CI):
;   MyAppVersion  e.g. 0.1.0
;   MyAppExeName  avidbackup.exe
;   DistDir       path to folder containing the exe

#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif
#ifndef MyAppExeName
  #define MyAppExeName "avidbackup.exe"
#endif
#ifndef DistDir
  #define DistDir "..\dist"
#endif

#define MyAppName "AvidBackup"
#define MyAppPublisher "Avid Web Solutions"
#define MyAppURL "https://avidwebsolutions.in"
#define MyAppAssocName "AvidBackup Account"
#define MyAppExeSource DistDir + "\\" + MyAppExeName

[Setup]
AppId={{A7B0C1D2-E3F4-4A5B-8C9D-0E1F2A3B4C5D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=AvidBackup-Setup-{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop icon"; GroupDescription: "Additional icons:"; Flags: unchecked
Name: "scheduledtask"; Description: "Install daily scheduled task (02:00)"; GroupDescription: "Automation:"; Flags: unchecked

[Files]
Source: "{#MyAppExeSource}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\examples\accounts.json.example"; DestDir: "{app}\examples"; Flags: ignoreversion
Source: "..\docs\*"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""$action = New-ScheduledTaskAction -Execute '{app}\{#MyAppExeName}'; $trigger = New-ScheduledTaskTrigger -Daily -At 2am; Register-ScheduledTask -TaskName 'AvidBackup-Daily' -Action $action -Trigger $trigger -Force"""; \
  Tasks: scheduledtask; Flags: runhidden skipifsilent

[UninstallRun]
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Unregister-ScheduledTask -TaskName 'AvidBackup-Daily' -Confirm:$false -ErrorAction SilentlyContinue"""; \
  Flags: runhidden; RunOnceId: "RemoveAvidBackupTask"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
