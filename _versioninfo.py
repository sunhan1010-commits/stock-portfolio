"""PyInstaller용 버전 리소스(version_info.txt) 생성기.

exe에 게시자/제품명/버전 같은 메타데이터를 심으면
백신 휴리스틱 오탐과 SmartScreen '알 수 없는 게시자' 경고가 다소 줄고,
경고 창에도 제품명이 표시된다. (서명은 아니므로 경고를 '완전히' 없애진 못함)

사용: python _versioninfo.py <출력경로>
"""
import sys
from version import APP_VERSION


def build(out_path: str):
    parts = (APP_VERSION.split(".") + ["0", "0", "0", "0"])[:4]
    a, b, c, d = (int(x) for x in parts)
    text = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({a}, {b}, {c}, {d}),
    prodvers=({a}, {b}, {c}, {d}),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([
      StringTable('041204b0', [
        StringStruct('CompanyName', 'Stock Portfolio Manager'),
        StringStruct('FileDescription', '주식 포트폴리오 매니저'),
        StringStruct('FileVersion', '{APP_VERSION}'),
        StringStruct('InternalName', 'StockPortfolio'),
        StringStruct('OriginalFilename', 'StockPortfolio.exe'),
        StringStruct('ProductName', '주식 포트폴리오 매니저'),
        StringStruct('ProductVersion', '{APP_VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [0x0412, 1200])])
  ]
)"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print("version_info written:", out_path)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "version_info.txt")
