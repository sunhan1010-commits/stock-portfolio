"""
GitHub Releases 기반 자동 업데이트.
- check_latest(): 최신 릴리스를 확인해 현재 버전보다 높으면 정보 dict 반환, 아니면 None.
- download_and_launch(url): 새 설치파일을 받아 실행(Inno Setup이 앱을 닫고 덮어씀).
네트워크/설정 오류는 조용히 무시(업데이트 확인 실패가 앱 실행을 막지 않음).
"""
from __future__ import annotations
import certs  # noqa: F401  SSL 인증서 환경 먼저 설정
import os
import re
import tempfile
import subprocess

import requests

from version import APP_VERSION, GITHUB_REPO

_HEADERS = {"User-Agent": "StockPortfolio-Updater",
            "Accept": "application/vnd.github+json"}


def _ver_tuple(s):
    nums = re.findall(r"\d+", str(s or ""))
    return tuple(int(x) for x in nums[:4]) or (0,)


def check_latest_verbose():
    """최신 릴리스 확인 -> 상태 dict.
    {'status': 'update'|'latest'|'error', ...}
      update: version,url,notes  /  latest: current,latest  /  error: error"""
    if not GITHUB_REPO or GITHUB_REPO == "OWNER/REPO":
        return {"status": "error", "error": "업데이트 저장소가 설정되지 않았습니다."}
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers=_HEADERS, timeout=8)
        if r.status_code != 200:
            return {"status": "error",
                    "error": f"GitHub 응답 오류 ({r.status_code})"}
        d = r.json()
        tag = d.get("tag_name") or d.get("name") or ""
        latest = tag.lstrip("vV")
        if _ver_tuple(tag) <= _ver_tuple(APP_VERSION):
            return {"status": "latest", "current": APP_VERSION, "latest": latest}
        url = None
        for a in d.get("assets", []):
            if (a.get("name") or "").lower().endswith(".exe"):
                url = a.get("browser_download_url")
                break
        if not url:
            return {"status": "error",
                    "error": "새 버전은 있으나 설치파일(.exe)이 아직 없습니다."}
        return {"status": "update", "version": latest, "url": url,
                "notes": (d.get("body") or "").strip()}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def check_latest():
    """최신 릴리스가 현재보다 높으면 {'version','url','notes'}, 아니면 None(하위호환)."""
    info = check_latest_verbose()
    return info if info.get("status") == "update" else None


def download_and_launch(url: str) -> bool:
    """설치파일을 임시폴더에 받아 실행. 성공 시 True (호출측에서 앱 종료)."""
    try:
        dest = os.path.join(tempfile.gettempdir(), "StockPortfolio_Update.exe")
        with requests.get(url, headers={"User-Agent": "StockPortfolio-Updater"},
                          stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
        # 설치 관리자 실행(사용자가 진행). 현재 앱은 호출측에서 곧 종료.
        subprocess.Popen([dest], close_fds=True)
        return True
    except Exception:
        return False
