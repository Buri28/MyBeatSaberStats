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

import json
import shutil
import sys
import tempfile
import threading
import zipfile
from pathlib import Path
from typing import Callable, NamedTuple

import requests
from PySide6.QtCore import QObject, QThread, Signal, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QTextEdit, QMessageBox,
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
_DEFAULT_MANAGED_INTERNAL_ROOTS = ("lib", "PySide6", "resources")

_API_BASE = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"


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


def _update_targets_file() -> Path:
    return _resources_dir() / _UPDATE_TARGETS_FILE


def _load_managed_internal_roots() -> tuple[str, ...]:
    """同期対象の _internal 直下ディレクトリ一覧を返す。"""
    path = _update_targets_file()
    try:
        data = json.loads(path.read_text("utf-8"))
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

def get_current_version() -> str | None:
    """version.json から現在インストール済みのバージョン文字列を読む。
    例: '1.0.0'  (先頭の 'v' は除去して格納)
    """
    vf = _version_file()
    if not vf.exists():
        return None
    try:
        data = json.loads(vf.read_text("utf-8"))
        ver = data.get("version")
        return str(ver).lstrip("v") if ver else None
    except Exception:
        return None


def save_current_version(version: str) -> None:
    """version.json にバージョンを書き込む (先頭 v は除去して保存)。"""
    _version_file().write_text(
        json.dumps({"version": version.lstrip("v")}, indent=2, ensure_ascii=False),
        "utf-8",
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


def check_for_updates() -> UpdateInfo:
    """GitHub Releases の latest を確認して UpdateInfo を返す。
    ネットワークエラー時は例外を送出する。
    """
    resp = requests.get(f"{_API_BASE}/releases/latest", timeout=10)
    resp.raise_for_status()
    release = resp.json()

    tag    = release["tag_name"]        # 例: "v1.0.1"
    notes  = release.get("body") or ""
    latest = tag.lstrip("v")            # 例: "1.0.1"
    current = get_current_version()
    release_zip_url = _find_release_zip_asset_url(release)

    if current is not None and not _version_gt(latest, current):
        # 同バージョンでもリソース再取得のためにファイル一覧を取得する
        files = _list_source_files_at_tag(tag)
        return UpdateInfo(
            has_update=False,
            current_version=current,
            latest_version=latest,
            latest_tag=tag,
            release_notes=notes,
            files=files,
            release_zip_url=release_zip_url,
        )

    # ダウンロード対象ファイル一覧をリリースタグの Git tree から取得
    files = _list_source_files_at_tag(tag)

    return UpdateInfo(
        has_update=True,
        current_version=current,
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


def _find_release_zip_asset_url(release: dict) -> str | None:
    """現在のアプリに対応する release zip のダウンロード URL を返す。"""
    prefix = _current_release_asset_prefix()
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
) -> None:
    """指定タグの .py ファイルおよびリソースファイルを全ダウンロードして保存する。
    progress(message, current, total) で進捗を通知する。
    """
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
        resp = requests.get(raw_url, timeout=30)
        resp.raise_for_status()

        content = resp.content
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


def _managed_internal_roots_for_update(info: UpdateInfo) -> set[str]:
    """今回の更新で release zip と同期すべき _internal 直下のディレクトリ名を返す。"""
    managed_roots = set(_load_managed_internal_roots())
    if not getattr(sys, "frozen", False) or not info.release_zip_url:
        return set()
    if info.has_update:
        return managed_roots
    if "resources" in managed_roots and any(path.startswith(_RESOURCES_PREFIX) for path in info.files):
        return {"resources"}
    return set()


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

    def run(self) -> None:  # type: ignore[override]
        try:
            self.finished.emit(check_for_updates())
        except Exception as e:
            self.error.emit(str(e))


class _DownloadThread(QThread):
    progress = Signal(str, int, int)  # message, current, total
    finished = Signal()
    error    = Signal(str)

    def __init__(self, info: UpdateInfo) -> None:
        super().__init__()
        self._info = info

    def run(self) -> None:  # type: ignore[override]
        try:
            apply_update(self._info, self._emit_progress)
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

        # ボタン
        btn_row = QHBoxLayout()
        self._update_btn = QPushButton(_update_btn_text)
        self._skip_btn   = QPushButton("スキップ")
        self._update_btn.clicked.connect(self._start_update)
        self._skip_btn.clicked.connect(self.reject)
        btn_row.addStretch(1)
        btn_row.addWidget(self._skip_btn)
        btn_row.addWidget(self._update_btn)
        layout.addLayout(btn_row)

    def _start_update(self) -> None:
        self._update_btn.setEnabled(False)
        self._skip_btn.setEnabled(False)

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

        self._download_thread = _DownloadThread(info)
        self._download_thread.progress.connect(self._on_progress)
        self._download_thread.finished.connect(self._on_finished)
        self._download_thread.error.connect(self._on_error)
        self._download_thread.start()

    def _on_progress(self, msg: str, current: int, total: int) -> None:
        self._progress_label.setText(msg)
        self._progress_bar.setMaximum(total)
        self._progress_bar.setValue(current)

    def _on_finished(self) -> None:
        self._progress_label.setText("完了しました！")
        QMessageBox.information(
            self,
            "アップデート完了",
            f"v{self._info.latest_version} へのアップデートが完了しました。\n"
            "アプリを再起動すると新しいバージョンが有効になります。",
        )
        self.accept()

    def _on_error(self, msg: str) -> None:
        self._update_btn.setEnabled(True)
        self._skip_btn.setEnabled(True)
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
