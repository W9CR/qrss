; Inno Setup script for QBS (qrss).  release\build-windows.ps1 passes the values:
;   /DAppVer=26.11.09a1 /DSourceDir=...\dist\QBS /DOutDir=...\out /DIconFile=...\qbs.ico

#ifndef AppVer
  #define AppVer "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\out\dist\QBS"
#endif
#ifndef OutDir
  #define OutDir "..\out"
#endif
#ifndef IconFile
  #define IconFile "..\out\icons\qbs.ico"
#endif

[Setup]
; Keep this AppId: it is what makes an upgrade replace the old install.
AppId={{9EA261BE-18CB-4AB0-B44B-E2A1A1AF45B1}
AppName=QBS
AppVersion={#AppVer}
AppVerName=QBS {#AppVer}
AppPublisher=Bryan Fields, W9CR
AppPublisherURL=https://github.com/W9CR/qrss
AppSupportURL=https://github.com/W9CR/qrss/issues
DefaultDirName={autopf}\QBS
DefaultGroupName=QBS
DisableProgramGroupPage=yes
; Per-user install: no administrator prompt, and {autopf} becomes the user's
; Local AppData\Programs.  The full bundle is self-contained.
PrivilegesRequired=lowest
OutputDir={#OutDir}
OutputBaseFilename=QBS-{#AppVer}-win64-setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\QBS.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\QBS"; Filename: "{app}\QBS.exe"
Name: "{group}\{cm:UninstallProgram,QBS}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\QBS"; Filename: "{app}\QBS.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\QBS.exe"; Description: "{cm:LaunchProgram,QBS}"; Flags: nowait postinstall skipifsilent
