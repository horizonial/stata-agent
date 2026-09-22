#ifndef CandidateRoot
  #error CandidateRoot must be provided by build_local_installer.py
#endif
#ifndef ReleaseId
  #error ReleaseId must be provided by build_local_installer.py
#endif
#ifndef OutputDir
  #error OutputDir must be provided by build_local_installer.py
#endif

#define ProductName "Stata Research Agent"
#define ProductExe "stata-research-agent.exe"
#define VersionRoot "{app}\\versions\\" + ReleaseId

[Setup]
AppId={{46BC6049-D4F1-461E-B1E0-8F40D35050CF}
AppName={#ProductName}
AppVersion={#ReleaseId}
AppPublisher=Stata Research Agent (local development)
DefaultDirName={localappdata}\Programs\StataResearchAgent
DefaultGroupName={#ProductName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=StataResearchAgent-{#ReleaseId}-local-unsigned
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#ProductName} (local unsigned rehearsal)
SetupLogging=yes

[Dirs]
Name: "{app}\launcher"
Name: "{app}\versions\{#ReleaseId}"

[Files]
Source: "{#CandidateRoot}\payload\*"; DestDir: "{app}\versions\{#ReleaseId}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#CandidateRoot}\evidence\*"; DestDir: "{app}\versions\{#ReleaseId}\evidence"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#ProductName}"; Filename: "{app}\versions\{#ReleaseId}\app\{#ProductExe}"; WorkingDir: "{app}\versions\{#ReleaseId}\app"
Name: "{userdesktop}\{#ProductName}"; Filename: "{app}\versions\{#ReleaseId}\app\{#ProductExe}"; WorkingDir: "{app}\versions\{#ReleaseId}\app"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[UninstallDelete]
Type: files; Name: "{app}\launcher\active-version.txt"
Type: dirifempty; Name: "{app}\launcher"
Type: dirifempty; Name: "{app}\versions"
Type: dirifempty; Name: "{app}"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    ForceDirectories(ExpandConstant('{app}\launcher'));
    SaveStringToFile(
      ExpandConstant('{app}\launcher\active-version.txt'),
      '{#ReleaseId}',
      False
    );
  end;
end;
