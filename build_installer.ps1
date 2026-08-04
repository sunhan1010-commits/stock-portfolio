# 주식 포트폴리오 매니저 - 빌드 & 설치파일 생성 스크립트
# 실행: PowerShell 에서  .\build_installer.ps1
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
# Google Drive 경로에 대량 파일 쓰기가 느리므로 빌드는 로컬 디스크에서 수행
$work = Join-Path $env:LOCALAPPDATA "Temp\spm_build"
$dist = Join-Path $work "dist"

Write-Host "== 1) PyInstaller 준비 ==" -ForegroundColor Cyan
& $py -m pip install --quiet --upgrade pyinstaller

Write-Host "== 1b) 버전 리소스 생성(백신 오탐/게시자표시 완화) ==" -ForegroundColor Cyan
New-Item -ItemType Directory -Force -Path $work | Out-Null
$verInfo = Join-Path $work "version_info.txt"
& $py "$PSScriptRoot\_versioninfo.py" "$verInfo"
if (-not (Test-Path $verInfo)) { throw "version_info.txt 생성 실패" }

Write-Host "== 2) 앱 빌드(PyInstaller onedir, 로컬) ==" -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean --windowed --name StockPortfolio `
  --distpath "$dist" --workpath "$work\build" `
  --version-file "$verInfo" `
  --collect-all curl_cffi `
  --collect-all FinanceDataReader `
  --collect-all pykrx `
  --collect-data certifi `
  --collect-submodules yfinance `
  --hidden-import requests_file --hidden-import bs4 --hidden-import lxml `
  --hidden-import multitasking --hidden-import frozendict `
  --hidden-import platformdirs --hidden-import pytz --hidden-import dateutil `
  app.py
$exe = Join-Path $dist "StockPortfolio\StockPortfolio.exe"
if (-not (Test-Path $exe)) { throw "빌드 실패: $exe 없음" }
Write-Host "빌드 완료: $exe" -ForegroundColor Green

Write-Host "== 3) Inno Setup 준비 ==" -ForegroundColor Cyan
$isccCandidates = @(
  "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
  "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
  "C:\Program Files\Inno Setup 6\ISCC.exe")
$iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
  Write-Host "Inno Setup 설치 중..." -ForegroundColor Yellow
  winget install -e --id JRSoftware.InnoSetup --source winget --accept-package-agreements --accept-source-agreements --disable-interactivity
  $iscc = $isccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $iscc) { throw "ISCC.exe 를 찾을 수 없습니다." }

Write-Host "== 4) 설치파일 컴파일 ==" -ForegroundColor Cyan
& $iscc "/DDISTDIR=$dist\StockPortfolio" "installer.iss"
$setup = Join-Path $PSScriptRoot "installer_output\StockPortfolio_Setup.exe"
if (Test-Path $setup) {
  Write-Host "`n완료! 설치파일: $setup" -ForegroundColor Green
} else {
  throw "설치파일 생성 실패"
}
