; CodingStudioUni Windows installer script for Inno Setup 6
; MyAppVersion is supplied on the command line (/DMyAppVersion=0.2.1) by CI.

#ifndef MyAppVersion
#define MyAppVersion "0.2.1"
#endif
#ifndef MyAppExeName
#define MyAppExeName "CodingStudioUni.exe"
#endif

#define MyAppName "CodingStudioUni"
#define MyAppPublisher "MicRaving"
#define MyAppURL "https://github.com/MicRaving/mcs"
#define SetupFileName "CodingStudioUni-{#MyAppVersion}-windows-x64-setup"

[Setup]
AppId={4F1A2B3C-5D6E-7F8A-9B0C-1D2E3F4A5B6C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={pf}\CodingStudio\CodingStudioUni
DefaultGroupName=CodingStudioUni
AllowNoIcons=yes
OutputDir=..
OutputBaseFilename={#SetupFileName}
Compression=lzma
SolidCompression=yes
PrivilegesRequired=admin

[Files]
Source: "CodingStudioUni\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\CodingStudioUni"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch CodingStudioUni"; Flags: nowait postinstall skipifsilent
