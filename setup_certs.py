"""
CA 번들 재생성 스크립트.
certs.py 가 로컬(LOCALAPPDATA\\StockPortfolioManager\\cacert.pem)에 번들을 만들어 쓴다.
SSL 인증서 오류가 나거나 회사/백신 인증서가 바뀌면 이 스크립트로 강제 재생성한다.
    .venv\\Scripts\\python.exe setup_certs.py
"""
import certs


def main():
    path = certs.ensure_bundle(force=True)
    if path:
        import os
        size = os.path.getsize(path)
        print(f"CA 번들 재생성 완료: {path} ({size:,} bytes)")
    else:
        print("CA 번들 생성 실패 — certifi/Windows 인증서를 확인하세요.")


if __name__ == "__main__":
    main()
