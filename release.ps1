# 주식 포트폴리오 매니저 - 새 버전 릴리스 자동화
# 사용: .\release.ps1 -Version 1.2.0 -Notes "변경 내용"
#  1) version.py / installer.iss 버전 갱신
#  2) build_installer.ps1 로 설치파일 빌드
#  3) git 커밋/푸시 + gh 릴리스 생성(setup.exe 첨부)
param(
  [Parameter(Mandatory = $true)][string]$Version,
  [string]$Notes = ""
)
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$gh = "C:\Program Files\GitHub CLI\gh.exe"

Write-Host "== 1) 버전 갱신 -> $Version ==" -ForegroundColor Cyan
(Get-Content version.py)  -replace 'APP_VERSION\s*=\s*"[^"]*"', "APP_VERSION = `"$Version`"" |
  Set-Content version.py -Encoding utf8
(Get-Content installer.iss) -replace 'AppVersion=.*', "AppVersion=$Version" |
  Set-Content installer.iss -Encoding utf8

Write-Host "== 2) 설치파일 빌드 ==" -ForegroundColor Cyan
& "$PSScriptRoot\build_installer.ps1"
$setup = Join-Path $PSScriptRoot "installer_output\StockPortfolio_Setup.exe"
if (-not (Test-Path $setup)) { throw "설치파일 생성 실패" }

Write-Host "== 3) git 커밋/푸시 ==" -ForegroundColor Cyan
git add version.py installer.iss
git commit -m "release v$Version" 2>&1 | Select-Object -Last 1
git push 2>&1 | Select-Object -Last 1

Write-Host "== 4) GitHub 릴리스 생성 ==" -ForegroundColor Cyan
if (-not $Notes) { $Notes = "주식 포트폴리오 매니저 v$Version" }
& $gh release create "v$Version" $setup --title "v$Version" --notes $Notes
Write-Host "`n완료! 릴리스가 게시되었습니다. 기존 사용자는 앱 실행 시 업데이트 안내를 받습니다." -ForegroundColor Green
