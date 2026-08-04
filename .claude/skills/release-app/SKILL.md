---
name: release-app
description: 주식 포트폴리오 매니저(이 프로젝트)의 새 버전을 빌드하고 GitHub에 릴리스해 자동 업데이트로 배포한다. 사용자가 "릴리스", "새 버전 배포", "업데이트 배포", "release", "exe 새로 내보내기" 등을 요청할 때 사용.
---

# 릴리스 / 자동 업데이트 배포 절차

이 프로젝트(`G:\내 드라이브\주식`, PySide6 데스크톱 앱)를 새 버전으로 빌드하고
GitHub Releases에 올려 기존 사용자에게 자동 업데이트로 배포한다.

## 사전 정보
- 저장소: `sunhan1010-commits/stock-portfolio` (public)
- gh CLI: `C:\Program Files\GitHub CLI\gh.exe` (계정 `sunhan1010-commits` 로그인 상태)
- 빌드 도구: PyInstaller(venv) + Inno Setup(`%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe`)
- 버전 위치: `version.py`의 `APP_VERSION`, `installer.iss`의 `AppVersion` (둘이 항상 일치해야 함)
- 자동 업데이트 로직: `updater.py`(GitHub releases/latest 확인) + `app.py` MainWindow 시작 시 확인

## 실행 방법 (한 방에)
새 버전 번호를 정해 `release.ps1` 실행:
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "G:\내 드라이브\주식\release.ps1" -Version 1.2.0 -Notes "변경 내용 요약"
```
이 스크립트가 순서대로 수행:
1. `version.py`·`installer.iss` 버전 갱신
2. `build_installer.ps1` → 로컬(`%LOCALAPPDATA%\Temp\spm_build`)에 PyInstaller 빌드 → ISCC로 `installer_output\StockPortfolio_Setup.exe` 생성
3. git 커밋/푸시
4. `gh release create v{버전} installer_output\StockPortfolio_Setup.exe` (설치파일 첨부)

빌드는 수 분 걸리므로 **백그라운드로 실행**하고 완료 통지를 기다린다.

## 수동으로 나눠서 할 때
1. `version.py` `APP_VERSION`, `installer.iss` `AppVersion`을 새 값으로 (UTF-8 유지).
2. `build_installer.ps1` 실행 → `installer_output\StockPortfolio_Setup.exe` 확인.
3. `& "C:\Program Files\GitHub CLI\gh.exe" release create v1.2.0 "installer_output\StockPortfolio_Setup.exe" --title "v1.2.0" --notes "..."`

## 검증
- 릴리스 후: `updater.check_latest()`를 낮은 APP_VERSION으로 테스트하면 새 버전과 `.exe` 에셋 URL이 잡혀야 함.
- 함정: PyInstaller `--specpath`를 C드라이브로 주지 말 것(G드라이브 소스와 교차드라이브 오류). 개인 데이터(`portfolio.db`)는 `.gitignore`로 커밋 제외.
- 함정(PowerShell 5.1): 네이티브 exe(git/gh)에 `2>&1 | ...` 파이프를 쓰면 정상 stderr가 NativeCommandError로 잡혀 `$ErrorActionPreference=Stop`에서 스크립트가 중단됨. release.ps1은 `git push`에 리다이렉트를 쓰지 않고 `$LASTEXITCODE`로 판정하도록 수정됨.
- 함정: release.ps1은 `git add -A`로 소스 전체를 커밋해야 함(과거 version 파일만 커밋해 기능 소스가 릴리스 커밋에 누락된 버그가 있었음 — 수정 완료).
