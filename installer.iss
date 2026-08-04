; 二쇱떇 ?ы듃?대━??留ㅻ땲? - Inno Setup ?ㅼ튂 ?ㅽ겕由쏀듃
; 而댄뙆?? ISCC.exe /DDISTDIR="<鍮뚮뱶 dist 寃쎈줈>" installer.iss  (build_installer.ps1 ???먮룞 ?섑뻾)
#ifndef DISTDIR
  #define DISTDIR "dist\StockPortfolio"
#endif

[Setup]
AppId={{9E7B2C40-5B49-4363-8914-596246B624DA}
AppName=二쇱떇 ?ы듃?대━??留ㅻ땲?
AppVersion=1.2.0
AppPublisher=Stock Portfolio Manager
DefaultDirName={autopf}\StockPortfolio
DefaultGroupName=二쇱떇 ?ы듃?대━??留ㅻ땲?
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
Name: desktopicon; Description: "諛뷀깢?붾㈃ 諛붾줈媛湲??앹꽦"; GroupDescription: "異붽? ?꾩씠肄?"

[Files]
Source: "{#DISTDIR}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\二쇱떇 ?ы듃?대━??留ㅻ땲?"; Filename: "{app}\StockPortfolio.exe"
Name: "{group}\二쇱떇 ?ы듃?대━??留ㅻ땲? ?쒓굅"; Filename: "{uninstallexe}"
Name: "{userdesktop}\二쇱떇 ?ы듃?대━??留ㅻ땲?"; Filename: "{app}\StockPortfolio.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\StockPortfolio.exe"; Description: "吏湲??ㅽ뻾"; Flags: nowait postinstall skipifsilent
