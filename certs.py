"""
SSL 인증서 설정.
이 PC는 보안 프로그램이 HTTPS를 검사(MITM)하며 자체 루트 인증서를 사용해,
Python 기본 certifi 번들로는 야후/네이버 연결 시 SSL 오류(CERTIFICATE_VERIFY_FAILED)가 난다.
=> certifi + Windows 신뢰 인증서를 합친 번들을 만들어 사용한다.

중요: 이 프로젝트 폴더는 Google Drive(G:\\내 드라이브) 위에 있는데, CA 번들 파일을
Drive 경로에 두면 requests가 매 연결마다 40만 바이트 파일을 Drive에서 읽어 ~11초씩
지연된다(스트리밍 파일). 따라서 번들은 반드시 로컬 디스크(LOCALAPPDATA)에 생성해 쓴다.

다른 모듈보다 먼저 import 되어야 한다.
"""
import os
import ssl
import tempfile

_APPDIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
    "StockPortfolioManager")
_BUNDLE = os.path.join(_APPDIR, "cacert.pem")


def _build_bundle(path: str) -> bool:
    try:
        import certifi
        parts = [open(certifi.where(), "r", encoding="utf-8").read()]
        seen = set()
        # Windows 신뢰 저장소(루트+중간). enum_certificates는 Windows 전용.
        for store in ("ROOT", "CA"):
            try:
                for der, enc, trust in ssl.enum_certificates(store):
                    if der in seen:
                        continue
                    seen.add(der)
                    try:
                        parts.append(ssl.DER_cert_to_PEM_cert(der))
                    except Exception:
                        pass
            except Exception:
                pass
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
        return True
    except Exception:
        return False


def ensure_bundle(force: bool = False) -> str | None:
    """로컬 CA 번들을 생성(필요 시)하고 경로를 반환."""
    try:
        if force or not os.path.exists(_BUNDLE) or os.path.getsize(_BUNDLE) < 1000:
            _build_bundle(_BUNDLE)
    except Exception:
        pass
    return _BUNDLE if os.path.exists(_BUNDLE) else None


CA_BUNDLE = ensure_bundle()

if CA_BUNDLE:
    # requests / urllib3 / curl_cffi(yfinance) 모두 이 로컬 번들을 쓰도록
    for _k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        os.environ[_k] = CA_BUNDLE
