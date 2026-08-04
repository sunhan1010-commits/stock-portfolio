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
# PowerShell 5.1의 Get-Content/Set-Content 기본 인코딩은 UTF-8 파일(한글 주석/AppName)을
# 깨뜨린다. .NET으로 UTF-8(BOM 유지) 읽기/쓰기 → 한글 보존.
function Update-VersionFile($path, $pattern, $replacement) {
  $full = Join-Path $PSScriptRoot $path
  $hasBom = $false
  $bytes = [System.IO.File]::ReadAllBytes($full)
  if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { $hasBom = $true }
  $text = [System.IO.File]::ReadAllText($full, [System.Text.Encoding]::UTF8)  # BOM 자동 인식/제거
  $text = [regex]::Replace($text, $pattern, $replacement)
  $enc = New-Object System.Text.UTF8Encoding($hasBom)   # 원본에 BOM 있었으면 유지
  [System.IO.File]::WriteAllText($full, $text, $enc)
}
Update-VersionFile "version.py"    'APP_VERSION\s*=\s*"[^"]*"' "APP_VERSION = `"$Version`""
Update-VersionFile "installer.iss" 'AppVersion=.*'             "AppVersion=$Version"

Write-Host "== 2) 설치파일 빌드 ==" -ForegroundColor Cyan
& "$PSScriptRoot\build_installer.ps1"
$setup = Join-Path $PSScriptRoot "installer_output\StockPortfolio_Setup.exe"
if (-not (Test-Path $setup)) { throw "설치파일 생성 실패" }

Write-Host "== 3) git 커밋/푸시 ==" -ForegroundColor Cyan
# 소스 전체를 스테이징(.gitignore가 개인데이터/빌드산출물 제외). version 파일만 커밋하던 버그 수정.
git add -A
# git 은 정상 진행 메시지도 stderr로 내보내므로 2>&1 파이프( NativeCommandError→중단 )를 쓰지 않는다.
$ErrorActionPreference = "Continue"
git commit -m "release v$Version"
if ($LASTEXITCODE -ne 0) { Write-Host "  (커밋할 변경이 없음 — 계속 진행)" -ForegroundColor Yellow }
git push
if ($LASTEXITCODE -ne 0) { $ErrorActionPreference = "Stop"; throw "git push 실패 (exit $LASTEXITCODE)" }
$ErrorActionPreference = "Stop"

Write-Host "== 4) GitHub 릴리스 생성 ==" -ForegroundColor Cyan
if (-not $Notes) { $Notes = "주식 포트폴리오 매니저 v$Version" }
$notesFile = Join-Path $env:TEMP "spm_release_notes.md"
$Notes | Out-File -FilePath $notesFile -Encoding utf8
& $gh release create "v$Version" $setup --title "v$Version" --notes-file $notesFile
Write-Host "`n완료! 릴리스가 게시되었습니다. 기존 사용자는 앱 실행 시 업데이트 안내를 받습니다." -ForegroundColor Green
