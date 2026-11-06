; PackDeploy installer — compile with Inno Setup 6 (build.bat does this for you)
; Copyright (C) 2026 NullHarp — GPLv3

#define AppName      "PackDeploy"
#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif
#define AppPublisher "NullHarp"
#define AppURL       "https://github.com/nullifiedharp/PackDeploy"
#define AppExe       "PackDeploy.exe"

[Setup]
; Keep this AppId the same forever, so updates install over the old version
AppId={{94C0293F-D784-41D8-9AE5-55D8B977F78D}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=installer
OutputBaseFilename={#AppName}-{#AppVersion}-Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#AppExe}
; Installs for the current user without admin; users can choose "all users" instead
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=yes
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "assoc"; Description: "Open .mcaddon and .mcpack files with {#AppName}"; GroupDescription: "File associations:"

[Files]
Source: "dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "LICENSE";        DestDir: "{app}"; Flags: ignoreversion
Source: "README.md";      DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}";  Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; .mcaddon / .mcpack -> "Open with PackDeploy" (adds to Open with list; becomes the default only if nothing else claims it)
Root: HKA; Subkey: "Software\Classes\.mcaddon\OpenWithProgids"; ValueType: string; ValueName: "PackDeploy.Addon"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\.mcpack\OpenWithProgids";  ValueType: string; ValueName: "PackDeploy.Addon"; ValueData: ""; Flags: uninsdeletevalue; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\PackDeploy.Addon"; ValueType: string; ValueName: ""; ValueData: "Minecraft Add-on"; Flags: uninsdeletekey; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\PackDeploy.Addon\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},0"; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\PackDeploy.Addon\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: assoc
; Right-click "Deploy with PackDeploy" — works even if Minecraft stays the default handler
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mcaddon\shell\PackDeploy"; ValueType: string; ValueName: ""; ValueData: "Deploy with {#AppName}"; Flags: uninsdeletekey; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mcaddon\shell\PackDeploy"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#AppExe},0"; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mcaddon\shell\PackDeploy\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mcpack\shell\PackDeploy"; ValueType: string; ValueName: ""; ValueData: "Deploy with {#AppName}"; Flags: uninsdeletekey; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mcpack\shell\PackDeploy"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\{#AppExe},0"; Tasks: assoc
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.mcpack\shell\PackDeploy\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: assoc

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
