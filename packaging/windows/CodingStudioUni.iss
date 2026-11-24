; CodingStudioUni Windows installer script for Inno Setup 6
; MyAppVersion is supplied on the command line (/DMyAppVersion=X.Y.Z) by CI,
; read from Scripts/coding_web/studio_lib.py (APP_VERSION) - the one source of
; truth; CI also refuses a tag that disagrees with it.  A local ISCC run
; without /D gets "0.0.0-dev", so a stale hard-coded number can never ship.

#ifndef MyAppVersion
#define MyAppVersion "0.0.0-dev"
#endif
#ifndef MyAppExeName
#define MyAppExeName "CodingStudioUni.exe"
#endif

#define MyAppName "CodingStudioUni"
#define MyAppPublisher "MicRaving"
#define MyAppURL "https://github.com/MicRaving/mcs"

[Setup]
AppId={{4F1A2B3C-5D6E-7F8A-9B0C-1D2E3F4A5B6C}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
; a 64-bit build belongs in "Program Files", not "Program Files (x86)"
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DefaultDirName={autopf}\CodingStudio\CodingStudioUni
UninstallDisplayIcon={app}\{#MyAppExeName}
; the in-app updater runs the next installer with /CLOSEAPPLICATIONS
CloseApplications=yes
DefaultGroupName=CodingStudioUni
AllowNoIcons=yes
OutputDir=..\..
OutputBaseFilename=CodingStudioUni-{#MyAppVersion}-windows-x64-setup
Compression=lzma
SolidCompression=yes
; Per-user by default (no admin prompt, and the in-app updater can replace it
; silently); the dialog still offers an all-users install.  An update keeps the
; mode of the previous install (UsePreviousPrivileges defaults to yes).
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Files]
Source: "..\..\CodingStudioUni\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\CodingStudioUni"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch CodingStudioUni"; Flags: nowait postinstall skipifsilent
