"""Qt のソースにある qt_attribution.json から、Qt に入っている第三者コンポーネントの
著作権表示とライセンス文を集め、licenses/qt-<版>-third-party.txt を作る。

Qt の版（PySide6 の版）を上げたときに1回だけ実行し、できたファイルをコミットする。
make_third_party_notices.py がビルドのたびにこのファイルを通知に入れる。
版が合わないとビルドが止まるので、そのときにこのスクリプトを実行し直す。

ソースの取り方（同梱している Qt のライブラリが入っているモジュールだけでよい）:
  https://download.qt.io/archive/qt/<x.y>/<x.y.z>/submodules/ から
  qtbase / qtsvg / qtimageformats の -everywhere-src-<x.y.z>.tar.xz を取り、.sha256 で確かめて展開する。

使い方:
  python make_qt_attributions.py <版> <qtbase の展開先> <qtsvg の展開先> <qtimageformats の展開先>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RULE = "-" * 78

# Windows 版の配布物（Qt6Core/Gui/Network/OpenGL/Svg/Widgets と同梱プラグイン）に入らないもの。
# ビルドの仕組み・テスト・他の OS 用・同梱していないモジュール（SQL・D-Bus）のコード。
# 迷うものは入れる側に倒す（多く載せても困らないが、足りないと表示の義務を満たせない）。
EXCLUDED_IDS = {
    "extra-cmake-modules", "kwin", "cmake-runcmake-test-modules",          # ビルドの仕組み
    "catch2", "cycle", "linuxperf", "valgrind",                            # Qt Test
    "android-native-style", "android-gradle-wrapper",                      # Android
    "qeventdispatcher_cf", "cocoa-platform-plugin",                        # macOS
    "forkfd",                                                              # Unix の Qt Core
    "dejayvu",                                                             # WebAssembly
    "xcb-xinput",                                                          # Linux の XCB プラグイン
    "pixman",                                                              # ARM NEON のみ
    "sqlite",                                                              # Qt SQL のプラグイン（同梱していない）
    "libdbus-1-headers",                                                   # Qt D-Bus（同梱していない）
}
EXCLUDED_PATH_PARTS = ("/tests/", "/examples/", "/wayland/")

# 渡すソース（この3つに同梱している Qt のライブラリが入っている）。
REQUIRED_MODULES = ("qtbase", "qtsvg", "qtimageformats")

# Apache-2.0 §4(d)：配布元に NOTICE があるものは、配布物に関係する部分を載せる。
# Apache Tika（Qt の tika-mimetypes の版 dbcbb368…）の NOTICE.txt から、tika-mimetypes.xml に関係する部分を写した。
# 取得元: https://raw.githubusercontent.com/apache/tika/dbcbb368e0a913e1a74cca61347fd6ce24bc6d7e/NOTICE.txt
# （残りは tika-server・tika-parsers などの依存の表示で、Qt に入っている mimetypes の定義には関係しない）
# emoji-segmenter（Apache-2.0）は 0.4.0 のリポジトリに NOTICE が無いことを確かめた。
EXTRA_NOTICES = {
    "tika-mimetypes": (
        "NOTICE (excerpt of the parts that pertain to tika-mimetypes.xml):\n"
        "\n"
        "Apache Tika\n"
        "Copyright 2021 The Apache Software Foundation\n"
        "\n"
        "This product includes software developed at\n"
        "The Apache Software Foundation (http://www.apache.org/).\n"
        "\n"
        "Tika-mimetypes.xml includes mimetype definitions that were adapted from the PRONOM Technical Registry\n"
        "by The National Archives (http://www.nationalarchives.gov.uk/PRONOM/Default.aspx). PRONOM is published\n"
        "under the Open Government License 3.0 (http://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)"
    ),
}


def _freetype_year(qtbase: Path) -> str:
    header = qtbase / "src" / "3rdparty" / "freetype" / "include" / "freetype" / "freetype.h"
    match = re.search(r"Copyright \(C\) \d{4}-(\d{4}) by", header.read_text(encoding="utf-8", errors="replace"))
    if not match:
        raise SystemExit(f"{header} から FreeType の年が読めません。")
    return match.group(1)


def required_statements(qtbase: Path) -> list[str]:
    """ライセンスが文書に書くよう求めている文。"""
    return [
        "Required acknowledgements:",
        "",
        "  This software is based in part on the work of the Independent JPEG Group.",
        "",
        f"  Portions of this software are copyright © {_freetype_year(qtbase)} The FreeType",
        "  Project (www.freetype.org).  All rights reserved.",
        "",
    ]


def _spdx_ids(expr: str) -> list[str]:
    ids = re.split(r"\s+(?:AND|OR|WITH)\s+|[()]", expr or "")
    return [i.strip() for i in ids if i.strip()]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _entries(src_roots: list[Path]):
    for src_root in src_roots:
        for json_path in sorted(src_root.rglob("qt_attribution.json")):
            rel = "/" + json_path.relative_to(src_root).as_posix()
            if any(part in rel for part in EXCLUDED_PATH_PARTS):
                continue
            data = json.loads(json_path.read_text(encoding="utf-8"), strict=False)
            for entry in data if isinstance(data, list) else [data]:
                if entry.get("Id") in EXCLUDED_IDS:
                    continue
                yield src_root, json_path.parent, entry


def build(version: str, src_roots: list[Path]) -> str:
    out = [
        f"Third-party components contained in Qt {version}",
        "",
        "Collected from qt_attribution.json in the Qt source code (qtbase, qtsvg,",
        "qtimageformats). Components used only on other platforms, only by the build",
        "system or tests, or only by Qt modules that are not distributed are omitted.",
        "",
        *required_statements(src_roots[0]),
    ]
    seen: set[str] = set()
    for src_root, base_dir, entry in _entries(src_roots):
        key = f"{entry.get('Id')}|{base_dir}"
        if key in seen:
            continue
        seen.add(key)
        name = entry.get("Name") or entry.get("Id")
        version_text = f" {entry['Version']}" if entry.get("Version") else ""
        out += [RULE, f"{name}{version_text}  ({src_root.name.split('-everywhere')[0]})"]
        if entry.get("Homepage"):
            out.append(entry["Homepage"])
        out.append(f"License: {entry.get('LicenseId') or entry.get('License')}")
        out += [RULE, ""]

        copyright_text = entry.get("Copyright")
        if isinstance(copyright_text, list):
            copyright_text = "\n".join(copyright_text)
        if copyright_text:
            out += [copyright_text.strip(), ""]
        if entry.get("CopyrightFile"):
            copyright_path = base_dir / entry["CopyrightFile"]
            if not copyright_path.exists():
                raise SystemExit(f"{copyright_path} がありません。")
            out += [_read(copyright_path), ""]

        license_files = entry.get("LicenseFiles") or ([entry["LicenseFile"]] if entry.get("LicenseFile") else [])
        texts: list[str] = []
        for rel in license_files:
            path = base_dir / rel
            if not path.exists():
                raise SystemExit(f"{path} がありません。")
            texts.append(_read(path))
        if not texts:
            for spdx in _spdx_ids(entry.get("LicenseId", "")):
                path = src_root / "LICENSES" / f"{spdx}.txt"
                if not path.exists():
                    raise SystemExit(f"{name} のライセンス文 {path} がありません。")
                texts.append(_read(path))
        if not texts:
            raise SystemExit(f"{name} のライセンス文が見つかりません。")
        for text in texts:
            out += [text, ""]
        if entry.get("Id") in EXTRA_NOTICES:
            out += [EXTRA_NOTICES[entry["Id"]], ""]
        out.append("")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    version = argv[0]
    src_roots = [Path(p) for p in argv[1:]]
    expected = [f"{m}-everywhere-src-{version}" for m in REQUIRED_MODULES]
    if [p.name for p in src_roots] != expected:
        raise SystemExit("ソースは次の3つをこの順で渡してください:\n  " + "\n  ".join(expected))
    for src_root in src_roots:
        if not src_root.is_dir():
            raise SystemExit(f"{src_root} がありません。")
    text = build(version, src_roots)
    out_path = ROOT / "licenses" / f"qt-{version}-third-party.txt"
    out_path.write_text(text + "\n", encoding="utf-8", newline="\r\n")
    print(f"  → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
