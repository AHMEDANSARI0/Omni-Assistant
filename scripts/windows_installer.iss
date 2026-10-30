; Self-Healing PC Agent per-user Windows installer template.
;
; This source script is intentionally unsigned. The canonical checked-in
; copy contains build tokens; `pc-agent release installer-source` replaces
; the version and explicit [Files] entries from its hash-manifested payload
; before Inno Setup compiles the generated script.
;
; This template is part of the payload so an installed copy remains
; inspectable, but the generated SelfHealingPCAgent.iss in an installer
; source package (not this token template) is the file to compile.

#define MyAppName "Self-Healing PC Agent"
#define MyAppVersion "@@PC_AGENT_VERSION@@"
#define MyAppPublisher "Self-Healing PC Agent project"

[Setup]
AppId={{C80BA7A3-9651-46C2-9D45-D1DADF2AD063}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Self-Healing PC Agent
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
MinVersion=10.0
OutputDir=output
OutputBaseFilename=SelfHealingPCAgent-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
ChangesEnvironment=no
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Dirs]
Name: "{app}\data"
Name: "{app}\workspace"

[Files]
@@PC_AGENT_FILES@@

[Icons]
Name: "{group}\Self-Healing PC Agent"; Filename: "{app}\scripts\launch_gui.cmd"; WorkingDir: "{app}"
Name: "{group}\Self-Healing PC Agent CLI"; Filename: "{app}\scripts\launch_cli.cmd"; WorkingDir: "{app}"
Name: "{autodesktop}\Self-Healing PC Agent"; Filename: "{app}\scripts\launch_gui.cmd"; WorkingDir: "{app}"; Tasks: desktopicon
Name: "{group}\Uninstall Self-Healing PC Agent"; Filename: "{uninstallexe}"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\install_windows.ps1"""; WorkingDir: "{app}"; StatusMsg: "Creating the Python environment and verifying the installation..."; Flags: waituntilterminated
Filename: "{app}\scripts\launch_gui.cmd"; Description: "Launch Self-Healing PC Agent"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent unchecked

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\uninstall_windows.ps1"" -RemoveVirtualEnvironment"; WorkingDir: "{app}"; RunOnceId: "RemovePcAgentVirtualEnvironment"; Flags: waituntilterminated

[UninstallDelete]
Type: dirifempty; Name: "{app}\data"
Type: dirifempty; Name: "{app}\workspace"
Type: dirifempty; Name: "{app}"
