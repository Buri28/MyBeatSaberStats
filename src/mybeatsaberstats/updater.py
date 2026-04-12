"""GitHub Releases からアプリのソースコードを更新するモジュール。

コア機能:
  - GitHub Releases API で最新リリース (latest) のタグを取得
  - ローカルの version.json のバージョンと比較 (v1.0.0 形式)
  - 新しいリリースがあれば src/mybeatsaberstats/*.py および resources/* を全ファイルダウンロード
  - PySide6 ダイアログで詳細・進捗表示

バージョン管理ルール:
  - タグ形式: v<major>.<minor>.<patch>  例: v1.0.0, v1.0.10
  - 比較は整数タプルで行う (文字列比較だと v1.0.9 > v1.0.10 になるため)

配布構成 (--onedir):
  MyBeatSaberStats/
  ├── MyBeatSaberStatsPlayer.exe   (ランチャー: Python+PySide6 同梱)
  └── _internal/
      ├── lib/mybeatsaberstats/    (差分更新対象の .py ファイル群)
      ├── resources/               (差分更新対象のリソースファイル群)
      └── version.json             (現在インストール済みのバージョン)

GitHub リリース手順:
  1. main ブランチに変更を push
  2. git tag v1.0.1 && git push origin v1.0.1
  3. GitHub で Release を作成 (タグ v1.0.1 を指定)
     → リリースノートが UpdateDialog に表示される

補足:
    - 通常は src/mybeatsaberstats/*.py と resources/* を raw URL から更新する
    - frozen 配布では release zip を正として _internal/lib、_internal/PySide6、
        _internal/resources を同期する
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Callable, NamedTuple
from urllib.parse import urlparse

import requests
from PySide6.QtCore import QObject, QThread, Signal, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QTextEdit, QMessageBox,
    QListWidget, QListWidgetItem, QLineEdit, QCheckBox, QWidget,
)

# ------------------------------------------------------------------ #
#  設定
# ------------------------------------------------------------------ #
GITHUB_OWNER = "Buri28"
GITHUB_REPO  = "MyBeatSaberStats"

# GitHub リポジトリ上でのソースコードの場所
_SOURCE_PREFIX    = "src/mybeatsaberstats/"
_RESOURCES_PREFIX = "resources/"
_UPDATE_TARGETS_FILE = "update_targets.json"
_UPDATER_EXE_NAME = "Update.exe"
_LEGACY_UPDATER_EXE_NAME = "MyBeatSaberUpdater.exe"
_PRESERVED_RELEASE_ROOT_FILES = frozenset({_UPDATER_EXE_NAME, _LEGACY_UPDATER_EXE_NAME})
_PRESERVED_RELEASE_ROOT_DIRS = frozenset({"cache", "snapshots"})
_DEFAULT_MANAGED_INTERNAL_ROOTS = ("lib", "PySide6", "resources")
_UNSAFE_INPLACE_INTERNAL_ROOTS = frozenset({"lib", "PySide6"})

_API_BASE = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"


def _read_json_text_with_bom_tolerance(path: Path) -> str:
    return path.read_text("utf-8-sig")


def _normalize_staged_file_bytes(rel_path: Path, content: bytes) -> bytes:
    suffix = rel_path.suffix.lower()
    normalized_parts = tuple(part.lower() for part in rel_path.parts)
    is_version_json = normalized_parts == ("_internal", "version.json")
    if suffix == ".py" or is_version_json:
        if content.startswith(b"\xef\xbb\xbf"):
            content = content[3:]
        content = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        content = content.replace(b"\n", b"\r\n")
    return content


def _format_release_published_at_local(published_at: str) -> str:
    value = str(published_at or "").strip()
    if not value:
        return ""
    try:
        utc_dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        local_dt = utc_dt.astimezone()
        offset = local_dt.utcoffset()
        if offset is None:
            return local_dt.strftime("%Y-%m-%d %H:%M:%S")
        total_minutes = int(offset.total_seconds() // 60)
        sign = "+" if total_minutes >= 0 else "-"
        abs_minutes = abs(total_minutes)
        hours, minutes = divmod(abs_minutes, 60)
        return f"{local_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC{sign}{hours}:{minutes:02d}"
    except ValueError:
        return value.replace("T", " ").replace("Z", " UTC")


# ------------------------------------------------------------------ #
#  バージョン比較ユーティリティ
# ------------------------------------------------------------------ #

def _parse_version(v: str) -> tuple[int, ...]:
    """'v1.0.10' または '1.0.10' を (1, 0, 10) に変換する。"""
    return tuple(int(x) for x in v.lstrip("v").split("."))


def _version_gt(a: str, b: str) -> bool:
    """a が b より新しいバージョンなら True。"""
    try:
        return _parse_version(a) > _parse_version(b)
    except ValueError:
        return False

# ------------------------------------------------------------------ #
#  パスユーティリティ
# ------------------------------------------------------------------ #

def _internal_dir() -> Path:
    """EXE 実行時は _MEIPASS (_internal/)、開発時はプロジェクトルート。"""
    if getattr(sys, "frozen", False):
        # PyInstaller 6.x: sys._MEIPASS = <exe の隣>/_internal/
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def _lib_dir() -> Path:
    """更新対象の mybeatsaberstats ディレクトリを返す。"""
    root = _internal_dir()
    if getattr(sys, "frozen", False):
        return root / "lib" / "mybeatsaberstats"
    return root / "src" / "mybeatsaberstats"


def _resources_dir() -> Path:
    """更新対象の resources ディレクトリを返す。"""
    return _internal_dir() / "resources"


def _version_file() -> Path:
    return _internal_dir() / "version.json"


def _version_file_for_install_dir(install_dir: Path | None = None) -> Path:
    if install_dir is None:
        return _version_file()
    return Path(install_dir) / "_internal" / "version.json"


def _update_targets_file() -> Path:
    return _resources_dir() / _UPDATE_TARGETS_FILE


def _load_managed_internal_roots() -> tuple[str, ...]:
    """同期対象の _internal 直下ディレクトリ一覧を返す。"""
    path = _update_targets_file()
    try:
        data = json.loads(_read_json_text_with_bom_tolerance(path))
        roots = data.get("internal_sync_dirs") or []
        normalized = tuple(str(root).strip() for root in roots if str(root).strip())
        if normalized:
            return normalized
    except Exception:
        pass
    return _DEFAULT_MANAGED_INTERNAL_ROOTS


# ------------------------------------------------------------------ #
#  バージョン管理
# ------------------------------------------------------------------ #

def get_current_version(install_dir: Path | None = None) -> str | None:
    """version.json から現在インストール済みのバージョン文字列を読む。
    例: '1.0.0'  (先頭の 'v' は除去して格納)
    """
    vf = _version_file_for_install_dir(install_dir)
    if not vf.exists():
        return None
    try:
        data = json.loads(_read_json_text_with_bom_tolerance(vf))
        ver = data.get("version")
        return str(ver).lstrip("v") if ver else None
    except Exception:
        return None


def save_current_version(version: str, install_dir: Path | None = None) -> None:
    """version.json にバージョンを書き込む (先頭 v は除去して保存)。"""
    vf = _version_file_for_install_dir(install_dir)
    vf.parent.mkdir(parents=True, exist_ok=True)
    vf.write_text(
        json.dumps({"version": version.lstrip("v")}, indent=2, ensure_ascii=False),
        "utf-8",
        newline="\r\n",
    )


# ------------------------------------------------------------------ #
#  GitHub Releases API
# ------------------------------------------------------------------ #

class UpdateInfo(NamedTuple):
    has_update:       bool
    current_version:  str | None   # 例: '1.0.0'  (None = version.json 未設定)
    latest_version:   str          # 例: '1.0.1'
    latest_tag:       str          # 例: 'v1.0.1'  (raw URL 取得に使用)
    release_notes:    str          # GitHub Release の body
    files:            list[str]    # "src/mybeatsaberstats/xxx.py" 形式
    release_zip_url:  str | None   # frozen 配布物の zip アセット URL


class ReleaseTagInfo(NamedTuple):
    tag_name: str
    title: str
    body: str
    published_at: str


def check_for_updates(
    *,
    target_tag: str | None = None,
    force_update: bool = False,
    asset_prefix: str | None = None,
    install_dir: Path | None = None,
) -> UpdateInfo:
    """GitHub Releases の latest を確認して UpdateInfo を返す。
    ネットワークエラー時は例外を送出する。
    """
    release = _fetch_release_by_tag(target_tag) if target_tag else _fetch_latest_release()
    current = get_current_version(install_dir=install_dir)
    return _build_update_info_from_release(
        release,
        current_version=current,
        force_update=force_update or bool(target_tag),
        asset_prefix=asset_prefix,
    )


def _fetch_latest_release() -> dict:
    resp = requests.get(f"{_API_BASE}/releases/latest", timeout=10)
    resp.raise_for_status()
    return resp.json()


def _fetch_release_by_tag(tag: str) -> dict:
    normalized_tag = str(tag).strip()
    if not normalized_tag:
        raise RuntimeError("target tag が空です。")
    if not normalized_tag.lower().startswith("v"):
        normalized_tag = f"v{normalized_tag}"
    resp = requests.get(f"{_API_BASE}/releases/tags/{normalized_tag}", timeout=10)
    if resp.status_code == 404:
        raise RuntimeError(f"指定タグのリリースが見つかりません: {normalized_tag}")
    resp.raise_for_status()
    return resp.json()


def list_release_tags(limit: int = 20) -> list[ReleaseTagInfo]:
    resp = requests.get(f"{_API_BASE}/releases", params={"per_page": limit}, timeout=10)
    resp.raise_for_status()

    releases: list[ReleaseTagInfo] = []
    for release in resp.json():
        if release.get("draft"):
            continue
        tag_name = str(release.get("tag_name") or "").strip()
        if not tag_name:
            continue
        releases.append(
            ReleaseTagInfo(
                tag_name=tag_name,
                title=str(release.get("name") or tag_name),
                body=str(release.get("body") or ""),
                published_at=str(release.get("published_at") or ""),
            )
        )
    return releases


def _build_update_info_from_release(
    release: dict,
    *,
    current_version: str | None,
    force_update: bool,
    asset_prefix: str | None,
) -> UpdateInfo:
    tag = str(release["tag_name"])
    notes = str(release.get("body") or "")
    latest = tag.lstrip("v")
    release_zip_url = _find_release_zip_asset_url(release, asset_prefix=asset_prefix)
    files = _list_source_files_at_tag(tag)

    has_update = force_update or current_version is None or _version_gt(latest, current_version)
    if current_version is not None and not force_update and not _version_gt(latest, current_version):
        has_update = False

    return UpdateInfo(
        has_update=has_update,
        current_version=current_version,
        latest_version=latest,
        latest_tag=tag,
        release_notes=notes,
        files=files,
        release_zip_url=release_zip_url,
    )


def _current_release_asset_prefix() -> str:
    """現在の実行アプリに対応する release zip の接頭辞を返す。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).stem
    return "MyBeatSaberStats"


def _find_release_zip_asset_url(release: dict, asset_prefix: str | None = None) -> str | None:
    """現在のアプリに対応する release zip のダウンロード URL を返す。"""
    prefix = asset_prefix or _current_release_asset_prefix()
    prefix_with_dash = f"{prefix}-"
    for asset in release.get("assets", []):
        name = str(asset.get("name") or "")
        if not name.lower().endswith(".zip"):
            continue
        if name == f"{prefix}.zip" or name.startswith(prefix_with_dash):
            url = asset.get("browser_download_url") or asset.get("url")
            if url:
                return str(url)
    return None


def _list_source_files_at_tag(tag: str) -> list[str]:
    """指定タグの src/mybeatsaberstats/ 以下の .py ファイルおよび
    resources/ 以下の全ファイル一覧を返す。"""
    resp = requests.get(
        f"{_API_BASE}/git/trees/{tag}?recursive=1", timeout=15
    )
    resp.raise_for_status()
    tree = resp.json().get("tree", [])
    src_files = [
        item["path"]
        for item in tree
        if item["path"].startswith(_SOURCE_PREFIX)
        and item["path"].endswith(".py")
        and item["type"] == "blob"
    ]
    res_files = [
        item["path"]
        for item in tree
        if item["path"].startswith(_RESOURCES_PREFIX)
        and item["type"] == "blob"
    ]
    return src_files + res_files


def apply_update(
    info: UpdateInfo,
    progress: Callable[[str, int, int], None] | None = None,
    *,
    preserve_updater: bool = False,
) -> None:
    """指定タグの .py ファイルおよびリソースファイルを全ダウンロードして保存する。
    progress(message, current, total) で進捗を通知する。
    """
    if _should_stage_external_update(info):
        _stage_external_update(info, progress, preserve_updater=preserve_updater)
        return

    lib   = _lib_dir()
    res   = _resources_dir()
    files = info.files
    managed_roots = _managed_internal_roots_for_update(info)
    needs_internal_sync = bool(managed_roots)
    total = len(files) + (1 if needs_internal_sync else 0)
    raw_base = (
        f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/{info.latest_tag}"
    )

    for i, repo_path in enumerate(files):
        if repo_path.startswith(_SOURCE_PREFIX):
            # src/mybeatsaberstats/ を除いたサブパスを維持する
            # 例: src/mybeatsaberstats/collector/beatleader.py → collector/beatleader.py
            rel_path = Path(repo_path).relative_to(_SOURCE_PREFIX)
            dest = lib / rel_path
        else:
            # resources/ を除いたサブパスを維持する
            # 例: resources/app_icon.ico → app_icon.ico
            rel_path = Path(repo_path).relative_to(_RESOURCES_PREFIX)
            dest = res / rel_path

        if progress:
            progress(f"ダウンロード中: {repo_path}", i + 1, total)

        raw_url = f"{raw_base}/{repo_path}"
        content = _read_update_source_bytes(raw_url, timeout=30)
        if repo_path.startswith(_SOURCE_PREFIX):
            # .py ファイルのみ: UTF-8 BOM 除去・改行コード CRLF 統一
            if content.startswith(b"\xef\xbb\xbf"):
                content = content[3:]
            content = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            content = content.replace(b"\n", b"\r\n")

        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    # 古い .pyc をすべて削除して新しい .py が確実に読まれるようにする
    for cache_dir in lib.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)

    if needs_internal_sync:
        if progress:
            progress("配布物の _internal を同期中...", len(files), total)
        _sync_internal_files_from_release_zip(
            str(info.release_zip_url),
            managed_roots,
            progress,
            total,
        )

    save_current_version(info.latest_version)


def _should_stage_external_update(info: UpdateInfo) -> bool:
    return bool(
        getattr(sys, "frozen", False)
        and info.has_update
        and info.release_zip_url
    )


def _stage_external_update(
    info: UpdateInfo,
    progress: Callable[[str, int, int], None] | None = None,
    *,
    preserve_updater: bool = False,
) -> None:
    """release zip を一時領域へ保存し、終了後適用する外部ヘルパーを起動する。"""
    if not info.release_zip_url:
        raise RuntimeError("release zip が見つからないため外部アップデートを開始できません。")

    total = 3
    stage_dir = Path(tempfile.mkdtemp(prefix="mybeatsaberstats-update-"))
    zip_path = stage_dir / "release.zip"

    if progress:
        progress("更新パッケージをダウンロード中...", 1, total)
    zip_path.write_bytes(_read_update_source_bytes(info.release_zip_url, timeout=120))

    install_dir = Path(sys.executable).resolve().parent
    updater_exe = _find_external_updater_executable(install_dir)

    if progress:
        progress("外部更新ヘルパーを準備中...", 2, total)
    if updater_exe is not None:
        launcher_exe = _copy_external_updater_executable(updater_exe)
        _launch_external_updater_executable(
            updater_exe=launcher_exe,
            zip_path=zip_path,
            stage_dir=stage_dir,
            install_dir=install_dir,
            exe_path=Path(sys.executable).resolve(),
            parent_pid=os.getpid(),
            target_version=info.latest_version,
            preserve_updater=preserve_updater,
        )
    else:
        script_path = stage_dir / "apply_update.ps1"
        script_path.write_text(
            _build_external_update_script(),
            encoding="utf-8",
        )
        _launch_external_update_helper(
            script_path=script_path,
            stage_dir=stage_dir,
            install_dir=install_dir,
            exe_path=Path(sys.executable).resolve(),
            parent_pid=os.getpid(),
        )

    if progress:
        progress("外部更新ヘルパーを起動しました。アプリ終了後に更新します...", 3, total)


def _find_external_updater_executable(install_dir: Path) -> Path | None:
    candidates = [
        install_dir / _UPDATER_EXE_NAME,
        Path(sys.executable).resolve().with_name(_UPDATER_EXE_NAME),
        install_dir / _LEGACY_UPDATER_EXE_NAME,
        Path(sys.executable).resolve().with_name(_LEGACY_UPDATER_EXE_NAME),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _copy_external_updater_executable(updater_exe: Path) -> Path:
    helper_dir = Path(tempfile.mkdtemp(prefix="mybeatsaberstats-updater-helper-"))
    helper_path = helper_dir / updater_exe.name
    shutil.copy2(updater_exe, helper_path)
    return helper_path


def _launch_external_updater_executable(
    updater_exe: Path,
    zip_path: Path,
    stage_dir: Path,
    install_dir: Path,
    exe_path: Path,
    parent_pid: int,
    target_version: str | None = None,
    preserve_updater: bool = False,
) -> None:
    creationflags = 0
    detached_process = getattr(subprocess, "DETACHED_PROCESS", 0)
    create_new_process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    creationflags |= detached_process | create_new_process_group | create_no_window

    subprocess.Popen(
        [
            str(updater_exe),
            "--apply-staged",
            "--zip",
            str(zip_path),
            "--install-dir",
            str(install_dir),
            "--exe-path",
            str(exe_path),
            "--wait-pid",
            str(parent_pid),
            "--target-version",
            str(target_version or ""),
            "--cleanup-dir",
            str(stage_dir),
        ] + (["--preserve-updater"] if preserve_updater else []),
        close_fds=True,
        creationflags=creationflags,
    )


def _build_external_update_script() -> str:
    return r"""
param(
    [Parameter(Mandatory=$true)][int]$ParentPid,
    [Parameter(Mandatory=$true)][string]$StageDir,
    [Parameter(Mandatory=$true)][string]$InstallDir,
    [Parameter(Mandatory=$true)][string]$ExePath
)

$ErrorActionPreference = 'Stop'

function Show-UpdateError([string]$Message) {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($Message, 'アップデートエラー', 'OK', 'Error') | Out-Null
}

try {
    if ($ParentPid -gt 0) {
        Wait-Process -Id $ParentPid -ErrorAction SilentlyContinue
    }

    $zipPath = Join-Path $StageDir 'release.zip'
    $extractDir = Join-Path $StageDir 'unzipped'

    if (Test-Path -LiteralPath $extractDir) {
        Remove-Item -LiteralPath $extractDir -Recurse -Force
    }

    Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force

    $sourceRoot = Get-ChildItem -LiteralPath $extractDir -Directory | Select-Object -First 1
    if (-not $sourceRoot) {
        throw '展開したリリースフォルダが見つかりません。'
    }

    & robocopy $sourceRoot.FullName $InstallDir /MIR /R:2 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed with exit code $LASTEXITCODE"
    }

    Remove-Item -LiteralPath $StageDir -Recurse -Force -ErrorAction SilentlyContinue
    Start-Process -FilePath $ExePath
}
catch {
    Show-UpdateError ("更新の適用に失敗しました:`n" + $_.Exception.Message)
}
""".lstrip()


def _launch_external_update_helper(
    script_path: Path,
    stage_dir: Path,
    install_dir: Path,
    exe_path: Path,
    parent_pid: int,
) -> None:
    creationflags = 0
    detached_process = getattr(subprocess, "DETACHED_PROCESS", 0)
    create_new_process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    creationflags |= detached_process | create_new_process_group

    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script_path),
            "-ParentPid",
            str(parent_pid),
            "-StageDir",
            str(stage_dir),
            "-InstallDir",
            str(install_dir),
            "-ExePath",
            str(exe_path),
        ],
        close_fds=True,
        creationflags=creationflags,
    )


def _read_update_source_bytes(source: str, timeout: int) -> bytes:
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https"):
        resp = requests.get(source, timeout=timeout)
        resp.raise_for_status()
        return resp.content
    if parsed.scheme == "file":
        return Path(parsed.path).read_bytes()
    source_path = Path(source)
    if source_path.exists():
        return source_path.read_bytes()
    resp = requests.get(source, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def _managed_internal_roots_for_update(info: UpdateInfo) -> set[str]:
    """今回の更新で release zip と同期すべき _internal 直下のディレクトリ名を返す。"""
    managed_roots = set(_load_managed_internal_roots())
    if not getattr(sys, "frozen", False) or not info.release_zip_url:
        return set()
    # frozen 配布の実行中は runtime 配下の DLL / pyd / ライブラリがロックされる。
    # それらをその場で上書きすると PermissionError になりやすいため、
    # インプレース更新では安全な resources のみ同期対象に残す。
    managed_roots -= _UNSAFE_INPLACE_INTERNAL_ROOTS
    if info.has_update:
        return managed_roots
    if "resources" in managed_roots and any(path.startswith(_RESOURCES_PREFIX) for path in info.files):
        return {"resources"}
    return set()


def _wait_for_process_exit(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return
        try:
            ctypes.windll.kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        return

    while True:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)


def _extract_release_root(zip_path: Path, extract_dir: Path) -> Path:
    if extract_dir.exists():
        shutil.rmtree(extract_dir, ignore_errors=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(extract_dir)

    directories = [path for path in extract_dir.iterdir() if path.is_dir()]
    if len(directories) == 1:
        return directories[0]
    if directories:
        return extract_dir
    raise RuntimeError("展開したリリースフォルダが見つかりません。")


def _mirror_directory(source_dir: Path, dest_dir: Path, *, preserve_updater: bool = False) -> None:
    expected_paths: set[Path] = set()
    dest_dir.mkdir(parents=True, exist_ok=True)

    for source_path in sorted(source_dir.rglob("*")):
        rel_path = source_path.relative_to(source_dir)
        if rel_path.parts and rel_path.parts[0] in _PRESERVED_RELEASE_ROOT_DIRS:
            continue
        if preserve_updater and len(rel_path.parts) == 1 and rel_path.name in _PRESERVED_RELEASE_ROOT_FILES:
            continue
        expected_paths.add(rel_path)
        dest_path = dest_dir / rel_path
        if source_path.is_dir():
            dest_path.mkdir(parents=True, exist_ok=True)
            continue
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        content = source_path.read_bytes()
        normalized = _normalize_staged_file_bytes(rel_path, content)
        if normalized != content:
            dest_path.write_bytes(normalized)
            shutil.copystat(source_path, dest_path)
        else:
            shutil.copy2(source_path, dest_path)

    for dest_path in sorted(dest_dir.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        rel_path = dest_path.relative_to(dest_dir)
        if rel_path.parts and rel_path.parts[0] in _PRESERVED_RELEASE_ROOT_DIRS:
            continue
        if len(rel_path.parts) == 1 and rel_path.name in _PRESERVED_RELEASE_ROOT_FILES:
            continue
        if rel_path in expected_paths:
            continue
        if dest_path.is_dir():
            shutil.rmtree(dest_path, ignore_errors=True)
        else:
            dest_path.unlink(missing_ok=True)


def apply_staged_update_package(
    zip_path: Path,
    install_dir: Path,
    *,
    exe_path: Path | None = None,
    wait_pid: int = 0,
    restart: bool = True,
    cleanup_dir: Path | None = None,
    target_version: str | None = None,
    preserve_updater: bool = False,
) -> None:
    _wait_for_process_exit(wait_pid)

    stage_root = cleanup_dir or zip_path.parent
    extract_dir = stage_root / "unzipped"
    release_root = _extract_release_root(zip_path, extract_dir)
    _mirror_directory(release_root, install_dir, preserve_updater=preserve_updater)
    if target_version:
        save_current_version(target_version, install_dir=install_dir)

    if cleanup_dir is not None:
        shutil.rmtree(cleanup_dir, ignore_errors=True)

    if restart and exe_path is not None:
        subprocess.Popen([str(exe_path)], close_fds=True)


def _default_cli_install_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _default_cli_exe_path(install_dir: Path) -> Path | None:
    preferred_names = ("MyBeatSaberStats.exe", "MyBeatSaberRanking.exe")
    for name in preferred_names:
        candidate = install_dir / name
        if candidate.exists():
            return candidate
    candidates = [
        path for path in install_dir.glob("*.exe")
        if path.name.lower() != _UPDATER_EXE_NAME.lower()
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _print_cli_payload(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def run_updater_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MyBeatSaberStats 外部アップデーター")
    parser.add_argument("--tag", help="更新対象の GitHub release tag。指定時はダウングレードも許可")
    parser.add_argument("--zip", dest="zip_source", help="適用する release zip の URL またはローカルパス")
    parser.add_argument("--install-dir", help="更新先のインストールディレクトリ")
    parser.add_argument("--exe-path", help="更新後に再起動する exe パス")
    parser.add_argument("--wait-pid", type=int, default=0, help="終了待ちする親プロセス ID")
    parser.add_argument("--no-restart", action="store_true", help="更新後にアプリを再起動しない")
    parser.add_argument("--dry-run", action="store_true", help="解決した更新内容だけ表示して終了")
    parser.add_argument("--preserve-updater", action="store_true", help="非常用: Updater を更新しない")
    parser.add_argument("--apply-staged", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--cleanup-dir", help=argparse.SUPPRESS)
    parser.add_argument("--target-version", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    install_dir = Path(args.install_dir).resolve() if args.install_dir else _default_cli_install_dir()
    exe_path = Path(args.exe_path).resolve() if args.exe_path else _default_cli_exe_path(install_dir)
    asset_prefix = exe_path.stem if exe_path is not None else None

    if args.apply_staged:
        if not args.zip_source:
            parser.error("--apply-staged には --zip が必要です")
        zip_path = Path(args.zip_source).resolve()
        payload = {
            "mode": "apply-staged",
            "zip": str(zip_path),
            "install_dir": str(install_dir),
            "exe_path": str(exe_path) if exe_path is not None else None,
            "wait_pid": args.wait_pid,
            "restart": not args.no_restart,
            "preserve_updater": args.preserve_updater,
        }
        if args.dry_run:
            _print_cli_payload(payload)
            return 0
        apply_staged_update_package(
            zip_path,
            install_dir,
            exe_path=exe_path,
            wait_pid=args.wait_pid,
            restart=not args.no_restart,
            cleanup_dir=Path(args.cleanup_dir).resolve() if args.cleanup_dir else None,
            target_version=args.target_version or None,
            preserve_updater=args.preserve_updater,
        )
        return 0

    resolved_info: UpdateInfo | None = None
    zip_source = args.zip_source
    if zip_source is None:
        resolved_info = check_for_updates(
            target_tag=args.tag,
            force_update=bool(args.tag),
            asset_prefix=asset_prefix,
            install_dir=install_dir,
        )
        zip_source = resolved_info.release_zip_url
        if not resolved_info.has_update and not args.dry_run:
            _print_cli_payload(
                {
                    "mode": "no-update",
                    "current_version": resolved_info.current_version,
                    "latest_version": resolved_info.latest_version,
                    "latest_tag": resolved_info.latest_tag,
                }
            )
            return 0

    if not zip_source:
        raise RuntimeError("release zip を特定できませんでした。--zip または --tag を指定してください。")

    payload = {
        "mode": "update",
        "install_dir": str(install_dir),
        "exe_path": str(exe_path) if exe_path is not None else None,
        "wait_pid": args.wait_pid,
        "restart": not args.no_restart,
        "zip": str(zip_source),
        "tag": args.tag,
        "current_version": resolved_info.current_version if resolved_info else get_current_version(install_dir=install_dir),
        "latest_version": resolved_info.latest_version if resolved_info else None,
        "latest_tag": resolved_info.latest_tag if resolved_info else None,
        "preserve_updater": args.preserve_updater,
    }
    if args.dry_run:
        _print_cli_payload(payload)
        return 0

    with tempfile.TemporaryDirectory(prefix="mybeatsaberstats-cli-update-") as temp_dir:
        zip_path = Path(temp_dir) / "release.zip"
        zip_path.write_bytes(_read_update_source_bytes(str(zip_source), timeout=120))
        apply_staged_update_package(
            zip_path,
            install_dir,
            exe_path=exe_path,
            wait_pid=args.wait_pid,
            restart=not args.no_restart,
            target_version=(resolved_info.latest_version if resolved_info else args.target_version),
            preserve_updater=args.preserve_updater,
        )
    return 0


def _sync_internal_files_from_release_zip(
    release_zip_url: str,
    managed_roots: set[str],
    progress: Callable[[str, int, int], None] | None,
    total: int,
) -> None:
    """release zip を正として _internal 配下の管理対象を同期する。"""
    internal_root = _internal_dir()
    with tempfile.TemporaryDirectory() as temp_dir:
        zip_path = Path(temp_dir) / "release.zip"
        resp = requests.get(release_zip_url, timeout=120)
        resp.raise_for_status()
        zip_path.write_bytes(resp.content)

        expected_files: dict[Path, zipfile.ZipInfo] = {}
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                rel_path = _release_member_to_internal_path(member.filename)
                if rel_path is None or rel_path.parts[0] not in managed_roots:
                    continue
                expected_files[rel_path] = member

            deleted = _delete_unexpected_internal_files(internal_root, set(expected_files))
            synced = 0
            for rel_path, member in expected_files.items():
                dest = internal_root / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                synced += 1

        if progress:
            progress(
                f"配布物の _internal を同期しました ({synced} 件更新, {deleted} 件削除)",
                total,
                total,
            )


def _delete_unexpected_internal_files(internal_root: Path, expected_files: set[Path]) -> int:
    """管理対象ディレクトリから release zip に存在しないファイルを削除する。"""
    deleted = 0
    managed_roots = {rel.parts[0] for rel in expected_files if rel.parts}
    for root_name in managed_roots:
        root = internal_root / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir():
                if path.name == "__pycache__" or not any(path.iterdir()):
                    shutil.rmtree(path, ignore_errors=True)
                continue
            rel_path = path.relative_to(internal_root)
            if rel_path not in expected_files:
                path.unlink(missing_ok=True)
                deleted += 1
        for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not any(path.iterdir()):
                path.rmdir()
    return deleted


def _release_member_to_internal_path(member_name: str) -> Path | None:
    """release zip 内のパスをローカル _internal 相対パスへ変換する。"""
    normalized = member_name.replace("\\", "/")
    for root_name in _load_managed_internal_roots():
        marker = f"/_internal/{root_name}/"
        if marker not in normalized:
            continue
        tail = normalized.split(marker, 1)[1].strip("/")
        if not tail:
            return None
        head = marker.removeprefix("/_internal/").strip("/")
        return Path(head) / Path(tail)
    return None


# ------------------------------------------------------------------ #
#  バックグラウンドスレッド
# ------------------------------------------------------------------ #

class _StartupCheckSignals(QObject):
    finished = Signal(object)  # UpdateInfo
    error = Signal(str)

class _CheckThread(QThread):
    finished = Signal(object)  # UpdateInfo
    error    = Signal(str)

    def __init__(
        self,
        *,
        target_tag: str | None = None,
        force_update: bool = False,
        asset_prefix: str | None = None,
        install_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self._target_tag = target_tag
        self._force_update = force_update
        self._asset_prefix = asset_prefix
        self._install_dir = install_dir

    def run(self) -> None:  # type: ignore[override]
        try:
            self.finished.emit(
                check_for_updates(
                    target_tag=self._target_tag,
                    force_update=self._force_update,
                    asset_prefix=self._asset_prefix,
                    install_dir=self._install_dir,
                )
            )
        except Exception as e:
            self.error.emit(str(e))


class _TagListThread(QThread):
    finished = Signal(object)  # list[ReleaseTagInfo]
    error = Signal(str)

    def __init__(self, limit: int = 20) -> None:
        super().__init__()
        self._limit = limit

    def run(self) -> None:  # type: ignore[override]
        try:
            self.finished.emit(list_release_tags(limit=self._limit))
        except Exception as e:
            self.error.emit(str(e))
class ReleaseTagPickerDialog(QDialog):
    def __init__(self, current_tag: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Tag指定アップデート")
        self.setMinimumWidth(560)
        self.setMinimumHeight(420)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        self._selected_tag: str | None = None
        self._initial_tag = current_tag.strip()
        self._releases: list[ReleaseTagInfo] = []
        self._thread: _TagListThread | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("GitHub Releases から tag を選択してください。"))

        self._filter_edit = QLineEdit("", self)
        self._filter_edit.setPlaceholderText("tag を絞り込み、または直接入力")
        self._filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter_edit)

        self._status_label = QLabel("候補を取得中...")
        layout.addWidget(self._status_label)

        self._list = QListWidget(self)
        self._list.itemDoubleClicked.connect(self._accept_selected_item)
        self._list.currentItemChanged.connect(self._sync_current_item_to_edit)
        layout.addWidget(self._list, 1)

        btn_row = QHBoxLayout()
        self._refresh_btn = QPushButton("再読込")
        self._select_btn = QPushButton("選択")
        self._cancel_btn = QPushButton("キャンセル")
        self._refresh_btn.clicked.connect(self._load_tags)
        self._select_btn.clicked.connect(self._accept_selection)
        self._cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(self._refresh_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._select_btn)
        layout.addLayout(btn_row)

        self._load_tags()

    def selected_tag(self) -> str | None:
        return self._selected_tag

    def _load_tags(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        self._refresh_btn.setEnabled(False)
        self._select_btn.setEnabled(False)
        self._status_label.setText("候補を取得中...")
        self._list.clear()
        self._thread = _TagListThread(limit=30)
        self._thread.finished.connect(self._on_tags_loaded)
        self._thread.error.connect(self._on_tags_error)
        self._thread.start()

    def _on_tags_loaded(self, releases: list[ReleaseTagInfo]) -> None:
        self._thread = None
        self._releases = releases
        self._refresh_btn.setEnabled(True)
        self._select_btn.setEnabled(True)
        self._status_label.setText(f"{len(releases)} 件の release を取得しました")
        self._apply_filter()
        self._select_initial_release()

    def _select_initial_release(self) -> None:
        if not self._initial_tag or self._list.count() == 0:
            return
        normalized = self._initial_tag
        if not normalized.lower().startswith("v"):
            normalized = f"v{normalized}"
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item is None:
                continue
            tag_name = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if tag_name.lower() == normalized.lower():
                self._list.setCurrentItem(item)
                return

    def _on_tags_error(self, msg: str) -> None:
        self._thread = None
        self._refresh_btn.setEnabled(True)
        self._select_btn.setEnabled(True)
        self._status_label.setText("候補の取得に失敗しました")
        QMessageBox.warning(self, "Tag指定アップデート", f"tag 候補の取得に失敗しました:\n{msg}")

    def _apply_filter(self) -> None:
        filter_text = self._filter_edit.text().strip().lower()
        self._list.clear()
        first_item: QListWidgetItem | None = None
        for release in self._releases:
            haystack = "\n".join((release.tag_name, release.title, release.published_at)).lower()
            if filter_text and filter_text not in haystack:
                continue
            subtitle = _format_release_published_at_local(release.published_at)
            label = release.tag_name
            if release.title and release.title != release.tag_name:
                label = f"{release.tag_name}  {release.title}"
            if subtitle:
                label = f"{label}\n{subtitle}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, release.tag_name)
            if release.body:
                item.setToolTip(release.body[:1000])
            self._list.addItem(item)
            if first_item is None:
                first_item = item
        if first_item is not None:
            self._list.setCurrentItem(first_item)
        if self._list.count() == 0:
            self._status_label.setText("一致する tag がありません。直接入力もできます")
        elif self._releases:
            self._status_label.setText(f"{self._list.count()} 件を表示中")

    def _sync_current_item_to_edit(self, current: QListWidgetItem | None, _previous: QListWidgetItem | None) -> None:
        if current is None:
            return

    def _accept_selected_item(self, item: QListWidgetItem) -> None:
        self._selected_tag = str(item.data(Qt.ItemDataRole.UserRole) or "").strip() or None
        if self._selected_tag:
            self.accept()

    def _accept_selection(self) -> None:
        current = self._list.currentItem()
        if current is not None:
            self._selected_tag = str(current.data(Qt.ItemDataRole.UserRole) or "").strip() or None
        if not self._selected_tag:
            self._selected_tag = self._filter_edit.text().strip() or None
        if not self._selected_tag:
            QMessageBox.information(self, "Tag指定アップデート", "tag を選択するか入力してください。")
            return
        self.accept()



class _DownloadThread(QThread):
    progress = Signal(str, int, int)  # message, current, total
    finished = Signal()
    error    = Signal(str)

    def __init__(self, info: UpdateInfo, *, preserve_updater: bool = False) -> None:
        super().__init__()
        self._info = info
        self._preserve_updater = preserve_updater

    def run(self) -> None:  # type: ignore[override]
        try:
            apply_update(self._info, self._emit_progress, preserve_updater=self._preserve_updater)
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))

    def _emit_progress(self, msg: str, current: int, total: int) -> None:
        self.progress.emit(msg, current, total)


# ------------------------------------------------------------------ #
#  更新ダイアログ
# ------------------------------------------------------------------ #

class UpdateDialog(QDialog):
    """利用可能な更新を表示して適用するかどうかを確認するダイアログ。"""

    def __init__(self, info: UpdateInfo, parent=None) -> None:
        super().__init__(parent)
        self._info = info
        self._download_thread: _DownloadThread | None = None
        self._tag_check_thread: _CheckThread | None = None
        self._tag_request_in_flight = False

        cur = info.current_version or "不明"
        if info.has_update:
            self.setWindowTitle(f"アップデート v{cur} → v{info.latest_version}")
            _header_text = (
                f"<b>新しいバージョンが利用可能です</b><br>"
                f"現在のバージョン: <b>v{cur}</b> &nbsp;→&nbsp; "
                f"最新バージョン: <b>v{info.latest_version}</b>"
            )
            _update_btn_text = "今すぐアップデート"
        else:
            self.setWindowTitle(f"リソースを再取得 (v{info.latest_version})")
            _header_text = (
                f"<b>現在のバージョンは最新です (v{cur})</b><br>"
                f"リソースファイルのみ再取得します。"
            )
            _update_btn_text = "リソースを再取得"
        self.setMinimumWidth(500)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        layout = QVBoxLayout(self)

        # バージョン表示
        layout.addWidget(QLabel(_header_text))

        # リリースノート
        if info.release_notes.strip():
            layout.addWidget(QLabel("リリースノート:"))
            notes = QTextEdit()
            notes.setReadOnly(True)
            notes.setMaximumHeight(150)
            notes.setPlainText(info.release_notes)
            layout.addWidget(notes)

        # 進捗表示
        self._progress_label = QLabel("")
        self._progress_bar   = QProgressBar()
        self._progress_bar.hide()
        self._progress_label.hide()
        layout.addWidget(self._progress_label)
        layout.addWidget(self._progress_bar)

        self._developer_toggle_btn = QPushButton("開発者モード...")
        self._developer_toggle_btn.clicked.connect(self._toggle_developer_options)
        layout.addWidget(self._developer_toggle_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        self._developer_options = QWidget(self)
        developer_layout = QVBoxLayout(self._developer_options)
        developer_layout.setContentsMargins(12, 8, 12, 8)
        developer_layout.setSpacing(6)
        warning_label = QLabel(
            "非常用オプションです。通常は使用しません。\n"
            "チェックすると Updater 自体は更新せず、現在の Updater を維持します。"
        )
        warning_label.setWordWrap(True)
        self._preserve_updater_checkbox = QCheckBox("非常用: Updater を更新しない")
        developer_layout.addWidget(warning_label)
        developer_layout.addWidget(self._preserve_updater_checkbox)
        self._developer_options.hide()
        layout.addWidget(self._developer_options)

        # ボタン
        btn_row = QHBoxLayout()
        self._tag_btn = QPushButton("Tag指定...")
        self._update_btn = QPushButton(_update_btn_text)
        self._skip_btn   = QPushButton("スキップ")
        self._tag_btn.clicked.connect(self._select_tag_for_test)
        self._update_btn.clicked.connect(self._start_update)
        self._skip_btn.clicked.connect(self.reject)
        btn_row.addStretch(1)
        btn_row.addWidget(self._tag_btn)
        btn_row.addWidget(self._skip_btn)
        btn_row.addWidget(self._update_btn)
        layout.addLayout(btn_row)

    def _start_update(self) -> None:
        self._set_action_buttons_enabled(False)

        if self._preserve_updater_checkbox.isChecked():
            if QMessageBox.warning(
                self,
                "開発者モード",
                "非常用オプションが有効です。\n"
                "Updater 自体は更新されません。通常は使用しません。\n"
                "このまま続行しますか？",
                QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            ) != QMessageBox.StandardButton.Ok:
                self._set_action_buttons_enabled(True)
                return

        # 同バージョン時はリソースファイルのみ再取得する
        if self._info.has_update:
            info = self._info
        else:
            res_files = [f for f in self._info.files if f.startswith(_RESOURCES_PREFIX)]
            info = self._info._replace(files=res_files)

        n = len(info.files)
        self._progress_bar.setMaximum(n)
        self._progress_bar.setValue(0)
        self._progress_bar.show()
        self._progress_label.show()

        self._download_thread = _DownloadThread(
            info,
            preserve_updater=self._preserve_updater_checkbox.isChecked(),
        )
        self._download_thread.progress.connect(self._on_progress)
        self._download_thread.finished.connect(self._on_finished)
        self._download_thread.error.connect(self._on_error)
        self._download_thread.start()

    def _toggle_developer_options(self) -> None:
        visible = not self._developer_options.isVisible()
        self._developer_options.setVisible(visible)
        self._developer_toggle_btn.setText("開発者モードを閉じる" if visible else "開発者モード...")

    def _set_action_buttons_enabled(self, enabled: bool) -> None:
        self._tag_btn.setEnabled(enabled and not self._tag_request_in_flight)
        self._update_btn.setEnabled(enabled and not self._tag_request_in_flight)
        self._skip_btn.setEnabled(enabled and not self._tag_request_in_flight)
        self._developer_toggle_btn.setEnabled(enabled and not self._tag_request_in_flight)
        self._preserve_updater_checkbox.setEnabled(enabled and not self._tag_request_in_flight)

    def _select_tag_for_test(self) -> None:
        if self._tag_check_thread is not None and self._tag_check_thread.isRunning():
            return

        picker = ReleaseTagPickerDialog(self._info.latest_tag or "v", self)
        if picker.exec() != int(QDialog.DialogCode.Accepted):
            return

        normalized = str(picker.selected_tag() or "").strip()
        if not normalized:
            QMessageBox.information(self, "Tag指定アップデート", "tag を入力してください。")
            return

        self._tag_request_in_flight = True
        self._set_action_buttons_enabled(False)
        self._progress_label.setText(f"tag {normalized} を確認中...")
        self._progress_label.show()
        self._progress_bar.hide()

        asset_prefix = Path(sys.executable).stem if getattr(sys, "frozen", False) else None
        install_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else None
        self._tag_check_thread = _CheckThread(
            target_tag=normalized,
            force_update=True,
            asset_prefix=asset_prefix,
            install_dir=install_dir,
        )
        self._tag_check_thread.finished.connect(self._on_tag_lookup_finished)
        self._tag_check_thread.error.connect(self._on_tag_lookup_error)
        self._tag_check_thread.start()

    def _on_tag_lookup_finished(self, info: UpdateInfo) -> None:
        self._tag_request_in_flight = False
        self._tag_check_thread = None
        self._progress_label.hide()
        self._progress_bar.hide()
        self._set_action_buttons_enabled(True)

        parent = self.parentWidget()
        self.accept()
        dlg = UpdateDialog(info, parent)
        dlg.exec()

    def _on_tag_lookup_error(self, msg: str) -> None:
        self._tag_request_in_flight = False
        self._tag_check_thread = None
        self._progress_label.hide()
        self._progress_bar.hide()
        self._set_action_buttons_enabled(True)
        QMessageBox.warning(self, "Tag指定アップデート", f"tag の確認に失敗しました:\n{msg}")

    def _on_progress(self, msg: str, current: int, total: int) -> None:
        self._progress_label.setText(msg)
        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(current)

    def _on_finished(self) -> None:
        self._progress_label.setText("完了しました！")
        staged_external = _should_stage_external_update(self._info)
        if staged_external:
            QMessageBox.information(
                self,
                "アップデート準備完了",
                f"v{self._info.latest_version} の更新を準備しました。\n"
                "このあとアプリを終了すると更新を適用し、自動で再起動します。",
            )
        else:
            QMessageBox.information(
                self,
                "アップデート完了",
                f"v{self._info.latest_version} へのアップデートが完了しました。\n"
                "アプリを再起動すると新しいバージョンが有効になります。",
            )
        self.accept()
        if staged_external:
            app = QApplication.instance()
            if app is not None:
                app.quit()

    def _on_error(self, msg: str) -> None:
        self._set_action_buttons_enabled(True)
        self._progress_bar.hide()
        self._progress_label.hide()
        QMessageBox.critical(self, "アップデートエラー", f"更新に失敗しました:\n{msg}")


# ------------------------------------------------------------------ #
#  起動時チェック統合ヘルパー
# ------------------------------------------------------------------ #

class StartupUpdateChecker:
    """起動時にバックグラウンドで更新確認を行い、結果をボタンに反映する。

    使い方::

        checker = StartupUpdateChecker(update_button, parent_widget)
        checker.start()
    """

    def __init__(self, button: QPushButton, parent_widget=None) -> None:
        self._button = button
        self._parent = parent_widget
        self._thread: threading.Thread | None = None
        self._info: UpdateInfo | None = None
        self._last_error: str | None = None
        self._manual_retry_requested = False
        self._signals = _StartupCheckSignals()
        self._signals.finished.connect(self._on_checked)
        self._signals.error.connect(self._on_error)
        self._button.clicked.connect(self._on_button_clicked)
        if parent_widget is not None and hasattr(parent_widget, "destroyed"):
            parent_widget.destroyed.connect(self._on_parent_destroyed)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._on_parent_destroyed)

    def start(self, *, manual: bool = False) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._info = None
        self._last_error = None
        self._manual_retry_requested = manual
        self._button.setEnabled(False)
        self._button.setText("🔄 確認中…")
        self._button.setToolTip("GitHub Releases を確認しています")
        # テキスト変更後にボタン幅を固定し、以降の文字列変化でレイアウトがズレないようにする
        self._button.setMinimumWidth(self._button.sizeHint().width())

        def _worker() -> None:
            try:
                self._signals.finished.emit(check_for_updates())
            except Exception as exc:  # noqa: BLE001
                self._signals.error.emit(str(exc))

        self._thread = threading.Thread(target=_worker, name="StartupUpdateChecker", daemon=True)
        self._thread.start()

    def _on_checked(self, info: UpdateInfo) -> None:
        self._info = info
        self._last_error = None
        self._manual_retry_requested = False
        if info.has_update:
            self._button.setText(f"🆕 v{info.latest_version}")
            self._button.setEnabled(True)
            self._button.setToolTip(f"v{info.latest_version} への更新があります")
        else:
            self._button.setText(f"✅ v{info.latest_version}")
            self._button.setEnabled(True)
            if info.files:
                self._button.setToolTip("最新版です。クリックでリソースを再取得できます")
            else:
                self._button.setToolTip("最新版です")
        self._thread = None

    def _on_error(self, _msg: str) -> None:
        self._info = None
        self._last_error = _msg
        self._button.setText("⚠ Retry Update")
        self._button.setEnabled(True)
        self._button.setToolTip(_msg)
        self._thread = None
        if self._manual_retry_requested:
            QMessageBox.warning(self._parent, "Update", f"更新チェックに失敗しました:\n{_msg}")
        self._manual_retry_requested = False

    def _on_button_clicked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._info is not None and (self._info.has_update or self._info.files):
            self._show_dialog(self._info)
            return
        self.start(manual=True)

    def _on_parent_destroyed(self, *_args) -> None:
        self._thread = None

    def _show_dialog(self, info: UpdateInfo) -> None:
        dlg = UpdateDialog(info, self._parent)
        dlg.exec()
