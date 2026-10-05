; Ember's Windows installer (Inno Setup 6). Wraps dist\Ember, which
; packaging/pyinstaller/build.py makes, into dist\Ember-Setup-x64.exe:
;
;     iscc /DVersion=1.3.0 packaging\windows\ember.iss
;
; Per user, no admin rights: installs to %LOCALAPPDATA%\Programs\Ember, adds a
; Start menu entry (and a desktop one if ticked) and an uninstaller in
; Settings > Apps. A running Ember is closed first (Restart Manager).
#ifndef Version
  #error pass the version: iscc /DVersion=x.y.z ember.iss
#endif

[Setup]
; never change AppId: it is how Windows knows an upgrade from a second app
AppId={{E518E529-583C-43B8-A3BB-A63B23DF2088}
AppName=Ember
AppVersion={#Version}
AppVerName=Ember {#Version}
AppPublisher=Pierre Beaucoral
AppPublisherURL=https://github.com/PierreBeaucoral/ember
AppUpdatesURL=https://github.com/PierreBeaucoral/ember/releases/latest
VersionInfoVersion={#Version}
PrivilegesRequired=lowest
DefaultDirName={autopf}\Ember
DisableProgramGroupPage=yes
DisableDirPage=auto
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\..\launchers\windows\claude-devtools.ico
UninstallDisplayIcon={app}\Ember.exe
OutputDir=..\..\dist
OutputBaseFilename=Ember-Setup-x64
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Tasks]
Name: desktopicon; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; an upgrade must not keep the previous version's libraries
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\..\dist\Ember\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Ember"; Filename: "{app}\Ember.exe"
Name: "{autodesktop}\Ember"; Filename: "{app}\Ember.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Ember.exe"; Description: "{cm:LaunchProgram,Ember}"; Flags: nowait postinstall skipifsilent
