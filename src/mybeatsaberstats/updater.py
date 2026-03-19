"""GitHub Releases からアプリのソースコードを更新するモジュール。

コア機能:
  - GitHub Releases API で最新リリース (latest) のタグを取得
  - ローカルの version.json のバージョンと比較 (v1.0.0 形式)
  - 新しいリリースがあれば src/mybeatsaberstats/*.py を全ファイルダウンロード
  - PySide6 ダイアログで詳細・進捗表示

バージョン管理ルール:
  - タグ形式: v<major>.<minor>.<patch>  例: v1.0.0, v1.0.10
  - 比較は整数タプルで行う (文字列比較だと v1.0.9 > v1.0.10 になるため)

配布構成 (--onedir):
  MyBeatSaberStats/
  ├── MyBeatSaberStatsPlayer.exe   (ランチャー: Python+PySide6 同梱)
  └── _internal/
      ├── lib/mybeatsaberstats/    (差分更新対象の .py ファイル群)
      ├── resources/
      └── version.json             (現在インストール済みのバージョン)

GitHub リリース手順:
  1. main ブランチに変更を push
  2. git tag v1.0.1 && git push origin v1.0.1
  3. GitHub で Release を作成 (タグ v1.0.1 を指定)
     → リリースノートが UpdateDialog に表示される
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Callable, NamedTuple

import requests
from PySide6.QtCore import QThread, Signal, Qt
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
_SOURCE_PREFIX = "src/mybeatsaberstats/"

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


def _version_file() -> Path:
    return _internal_dir() / "version.json"


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

    if current is not None and not _version_gt(latest, current):
        return UpdateInfo(
            has_update=False,
            current_version=current,
            latest_version=latest,
            latest_tag=tag,
            release_notes=notes,
            files=[],
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
    )


def _list_source_files_at_tag(tag: str) -> list[str]:
    """指定タグの src/mybeatsaberstats/ 以下の .py ファイル一覧を返す。"""
    resp = requests.get(
        f"{_API_BASE}/git/trees/{tag}?recursive=1", timeout=15
    )
    resp.raise_for_status()
    return [
        item["path"]
        for item in resp.json().get("tree", [])
        if item["path"].startswith(_SOURCE_PREFIX)
        and item["path"].endswith(".py")
        and item["type"] == "blob"
    ]


def apply_update(
    info: UpdateInfo,
    progress: Callable[[str, int, int], None] | None = None,
) -> None:
    """指定タグの .py ファイルを全ダウンロードして lib_dir に保存する。
    progress(message, current, total) で進捗を通知する。
    """
    lib   = _lib_dir()
    files = info.files
    total = len(files)
    raw_base = (
        f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/{info.latest_tag}"
    )

    for i, repo_path in enumerate(files):
        # src/mybeatsaberstats/ を除いたサブパスを維持する
        # 例: src/mybeatsaberstats/collector/beatleader.py → collector/beatleader.py
        rel_path = Path(repo_path).relative_to(_SOURCE_PREFIX)
        if progress:
            progress(f"ダウンロード中: {rel_path}", i + 1, total)

        raw_url = f"{raw_base}/{repo_path}"
        resp = requests.get(raw_url, timeout=30)
        resp.raise_for_status()

        content = resp.content
        # UTF-8 BOM を除去
        if content.startswith(b"\xef\xbb\xbf"):
            content = content[3:]
        # 改行コードを CRLF に統一 (まず LF に正規化してから CRLF へ変換)
        content = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        content = content.replace(b"\n", b"\r\n")

        dest = lib / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)

    # 古い .pyc をすべて削除して新しい .py が確実に読まれるようにする
    for cache_dir in lib.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)

    save_current_version(info.latest_version)


# ------------------------------------------------------------------ #
#  バックグラウンドスレッド
# ------------------------------------------------------------------ #

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
        self.setWindowTitle(f"アップデート v{cur} → v{info.latest_version}")
        self.setMinimumWidth(500)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        layout = QVBoxLayout(self)

        # バージョン表示
        layout.addWidget(QLabel(
            f"<b>新しいバージョンが利用可能です</b><br>"
            f"現在のバージョン: <b>v{cur}</b> &nbsp;→&nbsp; "
            f"最新バージョン: <b>v{info.latest_version}</b>"
        ))

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
        self._update_btn = QPushButton("今すぐアップデート")
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
        n = len(self._info.files)
        self._progress_bar.setMaximum(n)
        self._progress_bar.setValue(0)
        self._progress_bar.show()
        self._progress_label.show()

        self._download_thread = _DownloadThread(self._info)
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
        self._thread: _CheckThread | None = None

    def start(self) -> None:
        self._button.setEnabled(False)
        self._button.setText("🔄 確認中…")
        # テキスト変更後にボタン幅を固定し、以降の文字列変化でレイアウトがズレないようにする
        self._button.setMinimumWidth(self._button.sizeHint().width())
        self._thread = _CheckThread()
        self._thread.finished.connect(self._on_checked)
        self._thread.error.connect(self._on_error)
        self._thread.start()

    def _on_checked(self, info: UpdateInfo) -> None:
        if info.has_update:
            self._button.setText(f"🆕 v{info.latest_version}")
            self._button.setEnabled(True)
            # 多重接続を避けるため一度切断してから再接続
            try:
                self._button.clicked.disconnect()
            except RuntimeError:
                pass
            self._button.clicked.connect(lambda: self._show_dialog(info))
        else:
            self._button.setText(f"✅ v{info.latest_version}")
            self._button.setEnabled(True)

    def _on_error(self, _msg: str) -> None:
        self._button.setText("🔄 Update")
        self._button.setEnabled(True)

    def _show_dialog(self, info: UpdateInfo) -> None:
        dlg = UpdateDialog(info, self._parent)
        dlg.exec()
