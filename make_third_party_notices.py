"""配布物に入るサードパーティの著作権表示とライセンス文 (THIRD-PARTY-NOTICES.txt) を作る。

PyInstaller が残した build/<名前>/PYZ-00.toc と COLLECT-00.toc から、同梱したファイルの
取り込み元を1つずつ調べ、どのパッケージのものかを venv の RECORD で突き合わせる。
依存を足したり版を上げたりしても、ビルドし直せば通知も自動でそろう。

次のときはビルドを止める（exit 1）:
  - 取り込み元が分からないファイルがある（通知を書けないため）
  - GPLv3 のみの Qt モジュール（Qt Virtual Keyboard など）が入っている

使い方（build.ps1 から venv の python で呼ぶ）:
  python make_third_party_notices.py <build/<名前> フォルダ> <出力ファイル> [<出力ファイル> ...]
"""

from __future__ import annotations

import ast
import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LICENSES_DIR = ROOT / "licenses"

# Qt の無償版で GPLv3 だけで提供されるモジュール。同梱すると、アプリ全体を GPLv3 にする必要がある。
GPL_ONLY_QT_MARKERS = ("virtualkeyboard",)

# 同梱してよい Qt のライブラリ（LGPLv3 で提供されていることを Qt の文書で確かめたもの）。
# Qt には GPLv3 のみのモジュール（Virtual Keyboard、Charts など）があるので、
# ここに無い Qt6*.dll が入ったらビルドを止め、ライセンスを確かめてから足す。
ALLOWED_QT6_DLLS = {
    "qt6core.dll", "qt6gui.dll", "qt6network.dll", "qt6opengl.dll", "qt6svg.dll", "qt6widgets.dll",
}

# Microsoft の再頒布可能ファイル（VC++ ランタイム・Universal CRT）。表示の義務は無いが一覧に載せる。
# 名前だけでなく、ファイルの発行元が Microsoft であることも確かめる。
MS_RUNTIME_RE = re.compile(r"^(vcruntime\d+(_\d+)?|msvcp\d+(_\d+)?|ucrtbase|api-ms-win-[\w-]+)\.dll$", re.I)

QT_DISTS = {"pyside6", "pyside6_essentials", "pyside6_addons", "shiboken6"}
LICENSE_FILE_RE = re.compile(r"^(licen[cs]e|copying|notice|authors)", re.I)
RULE = "=" * 78


def _norm(path: str | Path) -> str:
    return str(Path(path).resolve()).lower()


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _under(path: str, folder: str) -> bool:
    """path が folder の中にあるか（_norm 済みの文字列どうし。名前の前方一致で誤らないよう区切りまで見る）。"""
    return path == folder or path.startswith(folder.rstrip("\\/") + "\\")


def _is_microsoft_file(path: str) -> bool:
    """PE のバージョン情報の CompanyName が Microsoft か。"""
    import pefile

    try:
        pe = pefile.PE(path, fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        for file_info in getattr(pe, "FileInfo", []) or []:
            for info in file_info:
                for table in getattr(info, "StringTable", []) or []:
                    company = table.entries.get(b"CompanyName", b"").decode("utf-8", "replace")
                    if "microsoft" in company.lower():
                        return True
    except Exception:
        return False
    return False


def app_version() -> str:
    try:
        import json

        return str(json.loads((ROOT / "version.json").read_text(encoding="utf-8-sig"))["version"])
    except Exception:
        return "?"


def load_bundled_sources(build_dir: Path) -> list[tuple[str, str]]:
    """(配布物の中の名前, 取り込み元のパス) を返す。"""
    entries: list[tuple[str, str]] = []
    for toc_name in ("PYZ-00.toc", "COLLECT-00.toc"):
        toc_path = build_dir / toc_name
        if not toc_path.exists():
            raise SystemExit(f"{toc_path} がありません。PyInstaller のビルドを先に行ってください。")
        data = ast.literal_eval(toc_path.read_text(encoding="utf-8"))
        items = data[1] if toc_name.startswith("PYZ") else data[0]
        for dest, src, _typecode in items:
            entries.append((str(dest), str(src or "")))
    return entries


def _dist_key(dist: metadata.Distribution) -> str:
    return f"{_canon(dist.metadata['Name'] or '')}=={dist.version}"


def build_file_owner_map() -> tuple[dict[str, metadata.Distribution], set[str]]:
    """venv にある各ファイルの絶対パス → それを入れたパッケージ。と、setuptools の中にあるパッケージの一覧。"""
    owners: dict[str, metadata.Distribution] = {}
    vendored_keys: set[str] = set()
    for dist in metadata.distributions():
        for file in dist.files or []:
            owners[_norm(dist.locate_file(file))] = dist
    # setuptools が中に持っているパッケージ（_vendor）は、setuptools ではなくそれぞれの持ち主にする。
    for dist in list(metadata.distributions()):
        if _canon(dist.metadata["Name"] or "") != "setuptools":
            continue
        vendor_dir = Path(dist.locate_file("setuptools/_vendor"))
        for info_dir in sorted(vendor_dir.glob("*.dist-info")):
            vendored = metadata.PathDistribution(info_dir)
            vendored_keys.add(_dist_key(vendored))
            for file in vendored.files or []:
                owners[_norm(info_dir.parent / file)] = vendored
    return owners, vendored_keys


def license_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    """パッケージに入っているライセンス文（ファイル名, 本文）。"""
    found: dict[str, Path] = {}
    for file in dist.files or []:
        parts = Path(str(file)).parts
        if not parts or not parts[0].endswith(".dist-info"):
            continue
        if LICENSE_FILE_RE.match(parts[-1]):
            found.setdefault(parts[-1], Path(dist.locate_file(file)))
    texts = []
    for name, path in sorted(found.items()):
        if path.exists():
            texts.append((name, path.read_text(encoding="utf-8", errors="replace").strip()))
    return texts


def license_label(dist: metadata.Distribution) -> str:
    md = dist.metadata
    expr = md.get("License-Expression")
    if expr:
        return expr
    lic = (md.get("License") or "").strip()
    if lic and "\n" not in lic and len(lic) <= 80:
        return lic
    for classifier in md.get_all("Classifier") or []:
        if classifier.startswith("License :: OSI Approved :: "):
            return classifier.rsplit(" :: ", 1)[1]
    return "see license text below"


def project_url(dist: metadata.Distribution) -> str:
    md = dist.metadata
    for entry in md.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in ("source", "source code", "repository", "code", "homepage", "home"):
            return url.strip()
    return (md.get("Home-page") or "").strip()


def qt_version() -> str:
    from PySide6 import QtCore

    return QtCore.qVersion()


def section(title: str, subtitle: str = "") -> str:
    lines = [RULE, title]
    if subtitle:
        lines.append(subtitle)
    lines.append(RULE)
    return "\n".join(lines) + "\n\n"


def qt_section(dists: list[metadata.Distribution], qt_files: list[str]) -> str:
    version = qt_version()
    major_minor = ".".join(version.split(".")[:2])
    pyside = next((d for d in dists if _canon(d.metadata["Name"]) == "pyside6"), dists[0])
    pyside_version = pyside.version
    names = ", ".join(sorted(f"{d.metadata['Name']} {d.version}" for d in dists))
    modules = sorted({Path(f).name for f in qt_files if Path(f).name.lower().startswith("qt6")})
    body = [
        f"Qt {version} libraries and Qt for Python ({names})",
        "are used under the GNU Lesser General Public License version 3 (LGPLv3).",
        "LGPLv3 is a set of additional permissions on top of the GNU General Public",
        "License version 3, so the full texts of both licenses are included below.",
        "",
        "Qt modules shipped in _internal\\PySide6\\:",
        "  " + ", ".join(modules),
        "",
        "The Qt and PySide6 libraries are unmodified and are shipped as separate files",
        "(DLL / .pyd) in _internal\\PySide6\\ and _internal\\shiboken6\\. You may replace",
        "them with your own compatible builds.",
        "",
        "Corresponding source code:",
        f"  Qt {version}:      https://download.qt.io/archive/qt/{major_minor}/{version}/single/",
        f"  PySide6 {pyside_version}: https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{pyside_version}-src/",
        "",
        "Qt contains third-party components under their own licenses. Their copyright",
        "notices and license texts follow the GPL / LGPL texts below. See also",
        f"\"Licenses Used in Qt\": https://doc.qt.io/qt-{major_minor}/licenses-used-in-qt.html",
        "",
        "Qt is a trademark of The Qt Company Ltd.",
    ]
    out = section("Qt / Qt for Python (PySide6, Shiboken6)", "LGPL-3.0-only") + "\n".join(body) + "\n\n"
    for file_name, title in (("lgpl-3.0.txt", "GNU LESSER GENERAL PUBLIC LICENSE Version 3"),
                             ("gpl-3.0.txt", "GNU GENERAL PUBLIC LICENSE Version 3")):
        path = LICENSES_DIR / file_name
        if not path.exists():
            raise SystemExit(f"{path} がありません。")
        out += section(title, f"(licenses\\{file_name})") + path.read_text(encoding="utf-8").strip() + "\n\n\n"
    # Qt の中の第三者コンポーネント（libjpeg・libpng・freetype など）。make_qt_attributions.py で作ったもの。
    attributions = LICENSES_DIR / f"qt-{version}-third-party.txt"
    if not attributions.exists():
        raise SystemExit(
            f"{attributions} がありません。Qt {version} のソースから make_qt_attributions.py で作ってください。"
        )
    out += RULE + "\n" + attributions.read_text(encoding="utf-8").strip() + "\n\n\n"
    return out


def python_section() -> str:
    license_path = Path(sys.base_prefix) / "LICENSE.txt"
    if not license_path.exists():
        raise SystemExit(f"{license_path} がありません（Python の LICENSE.txt）。")
    version = ".".join(str(v) for v in sys.version_info[:3])
    return (
        section(f"Python {version} (python3*.dll, standard library, extension modules)",
                "PSF-2.0 and the licenses of the components bundled with Python "
                "(OpenSSL, libffi, bzip2, Zstandard, ...)")
        + license_path.read_text(encoding="utf-8", errors="replace").strip()
        + "\n\n\n"
    )


def dist_section(dist: metadata.Distribution, vendored: bool) -> str:
    name = dist.metadata["Name"]
    title = f"{name} {dist.version}" + (" (bundled inside setuptools)" if vendored else "")
    subtitle = license_label(dist)
    url = project_url(dist)
    if url:
        subtitle += f"  {url}"
    # MPL-2.0 などは、実行形式で配るときにソースの入手先を示す必要がある。版を固定した PyPI のページを書く。
    subtitle += f"\nSource Code Form: https://pypi.org/project/{name}/{dist.version}/"
    if vendored:
        subtitle += " (as vendored in setuptools)"
    texts = license_texts(dist)
    if not texts:
        raise SystemExit(f"{name} {dist.version} のライセンス文が見つかりません。通知を手で足してください。")
    out = section(title, subtitle)
    for file_name, text in texts:
        if len(texts) > 1:
            out += f"--- {file_name} ---\n\n"
        out += text + "\n\n"
    return out + "\n"


def generate(build_dir: Path) -> str:
    entries = load_bundled_sources(build_dir)
    owners, all_vendored = build_file_owner_map()
    base_prefix = _norm(sys.base_prefix)
    root = _norm(ROOT)
    venv = _norm(sys.prefix)

    gpl_only = [dest for dest, _ in entries if any(m in dest.lower() for m in GPL_ONLY_QT_MARKERS)]
    if gpl_only:
        raise SystemExit("GPLv3 のみの Qt モジュールが入っています。spec で外してください:\n  " + "\n  ".join(gpl_only))
    unchecked_qt = sorted({
        Path(dest).name for dest, _ in entries
        if re.match(r"^qt6\w*\.dll$", Path(dest).name, re.I) and Path(dest).name.lower() not in ALLOWED_QT6_DLLS
    })
    if unchecked_qt:
        raise SystemExit(
            "ライセンスを確かめていない Qt のライブラリが入っています。GPLv3 のみでないかを Qt の文書で確かめ、"
            "ALLOWED_QT6_DLLS に足すか spec で外してください:\n  " + "\n  ".join(unchecked_qt)
        )

    used: dict[str, metadata.Distribution] = {}
    vendored_keys: set[str] = set()
    qt_files: list[str] = []
    uses_python = False
    ms_runtime: set[str] = set()
    unknown: list[str] = []

    for dest, src in entries:
        if not src or src == "-":
            continue  # PyInstaller が中で作るもの（取り込み元ファイルが無い）
        key = _norm(src)
        dist = owners.get(key)
        if dist is not None:
            dist_key = _dist_key(dist)
            used[dist_key] = dist
            if dist_key in all_vendored:
                vendored_keys.add(dist_key)
            if _canon(dist.metadata["Name"]) in QT_DISTS:
                qt_files.append(dest)
            continue
        if MS_RUNTIME_RE.match(Path(dest).name) and _is_microsoft_file(src):
            ms_runtime.add(Path(dest).name)
            continue
        if _under(key, base_prefix):
            uses_python = True
            continue
        if _under(key, root) and not _under(key, venv):
            continue  # このアプリ自身のファイル（src・resources・version.json・exe など）
        unknown.append(f"{dest}  <-  {src}")

    if unknown:
        raise SystemExit("取り込み元が分からないファイルがあります。通知を書けないので止めます:\n  " + "\n  ".join(unknown))

    qt = [d for k, d in used.items() if _canon(d.metadata["Name"]) in QT_DISTS]
    # PyInstaller のブートローダー（exe 本体）は build/・dist/ から来るので上の突き合わせでは拾えない。常に載せる。
    pyinstaller = [d for d in metadata.distributions() if _canon(d.metadata["Name"] or "") == "pyinstaller"][:1]
    if not pyinstaller:
        raise SystemExit("PyInstaller のパッケージ情報が見つかりません。")
    others = sorted(
        ((k, d) for k, d in used.items() if _canon(d.metadata["Name"]) not in QT_DISTS | {"pyinstaller"}),
        key=lambda kd: (kd[0] in vendored_keys, kd[0]),
    )

    summary = []
    if qt:
        summary.append(f"  - Qt {qt_version()} / PySide6 {qt[0].version} (LGPL-3.0)")
    if uses_python:
        summary.append(f"  - Python {'.'.join(str(v) for v in sys.version_info[:3])} (PSF-2.0 and bundled components)")
    for key, dist in others:
        mark = " [bundled inside setuptools]" if key in vendored_keys else ""
        summary.append(f"  - {dist.metadata['Name']} {dist.version} ({license_label(dist)}){mark}")
    for dist in pyinstaller:
        summary.append(f"  - PyInstaller {dist.version} bootloader and runtime hooks "
                       "(GPL-2.0-or-later with the PyInstaller bootloader exception)")
    if ms_runtime:
        summary.append("  - Microsoft Visual C++ Runtime / Universal C Runtime (Microsoft redistributable files)")

    out = [
        "MyBeatSaberStats - Third-party notices",
        f"(for the files distributed with MyBeatSaberStats {app_version()})",
        "",
        "MyBeatSaberStats itself is licensed under the MIT License (see the LICENSE file",
        "next to MyBeatSaberStats.exe or in _internal\\resources\\, or",
        "https://github.com/Buri28/MyBeatSaberStats).",
        "This distribution contains the following third-party software.",
        "",
        *summary,
        "",
        "",
    ]
    text = "\n".join(out)
    if qt:
        text += qt_section(qt, qt_files)
    if uses_python:
        text += python_section()
    for _key, dist in others:
        text += dist_section(dist, vendored=_key in vendored_keys)
    for dist in pyinstaller:
        text += dist_section(dist, vendored=False)
    if ms_runtime:
        text += section("Microsoft Visual C++ Runtime / Universal C Runtime") + (
            "The following files are Microsoft redistributable runtime files, distributed\n"
            "under the terms that Microsoft provides for redistribution:\n  "
            + ", ".join(sorted(ms_runtime, key=str.lower)) + "\n"
        )
    return text


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    build_dir = Path(argv[0])
    text = generate(build_dir)
    for out in argv[1:]:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8", newline="\r\n")
        print(f"  THIRD-PARTY-NOTICES.txt → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
