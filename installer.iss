; 주식 포트폴리오 매니저 - Inno Setup 설치 스크립트
; 빌드: ISCC.exe /DDISTDIR="<빌드된 dist 폴더>" installer.iss  (build_installer.ps1 이 자동 실행)
#ifndef DISTDIR
  #define DISTDIR "dist\StockPortfolio"
#endif

[Setup]
AppId={{9E7B2C40-5B49-4363-8914-596246B624DA}
AppName=주식 포트폴리오 매니저
AppVersion=1.3.5
AppPublisher=Stock Portfolio Manager
DefaultDirName={autopf}\StockPortfolio
DefaultGroupName=주식 포트폴리오 매니저
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=StockPortfolio_Setup
Compression=lzma2/max
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
WizardStyle=modern
PrivilegesRequired=lowest

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: desktopicon; Description: "바탕화면 바로가기 만들기"; GroupDescription: "추가 아이콘:"

[Files]
Source: "{#DISTDIR}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\주식 포트폴리오 매니저"; Filename: "{app}\StockPortfolio.exe"
Name: "{group}\주식 포트폴리오 매니저 제거"; Filename: "{uninstallexe}"
Name: "{userdesktop}\주식 포트폴리오 매니저"; Filename: "{app}\StockPortfolio.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\StockPortfolio.exe"; Description: "지금 실행"; Flags: nowait postinstall skipifsilent
