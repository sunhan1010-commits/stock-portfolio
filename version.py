"""앱 버전 및 자동 업데이트 설정."""

# 릴리스할 때마다 올린다 (installer.iss 의 AppVersion 과 맞출 것)
APP_VERSION = "1.4.2"

# 자동 업데이트용 GitHub 저장소 "OWNER/REPO"  (public 이면 토큰 불필요)
# 예: "sunhan1010/stock-portfolio"  — 저장소를 만들고 여기에 입력하세요.
# 비워두거나 미설정이면 업데이트 확인은 조용히 건너뜁니다(앱은 정상 동작).
GITHUB_REPO = "sunhan1010-commits/stock-portfolio"
