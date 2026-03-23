"""MyBeatSaberStats パッチ適用プログラム。

配布構成:
  MyBeatSaberStats/
  ├── MyBeatSaberStatsPlayer.exe
  ├── apply_patch.exe        ← このプログラム
  ├── _internal/             ← メインアプリの _internal（更新対象）
  │   ├── lib/mybeatsaberstats/
  │   ├── resources/
  │   └── version.json
  └── patch/                 ← パッチ内容（同フォルダに展開しておく）
      ├── lib/
      │   └── mybeatsaberstats/
      │       ├── *.py
      │       └── collector/*.py
      ├── resources/          ← アイコン等（存在する場合のみ適用）
      │   └── *.ico, *.png, ...
      └── version.json

使い方:
  1. patch/ フォルダを apply_patch.exe と同じフォルダに置く
  2. apply_patch.exe を実行してパッチを適用する
  3. メインアプリを再起動する
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel,
    QMessageBox, QProgressBar, QPushButton, QVBoxLayout,
)


# ------------------------------------------------------------------ #
#  パスユーティリティ
# ------------------------------------------------------------------ #

def _patcher_dir() -> Path:
    """apply_patch.exe があるフォルダ（frozen 時は exe の親、開発時はスクリプトの親）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _patch_dir() -> Path:
    return _patcher_dir() / "patch"


def _internal_dir() -> Path:
    return _patcher_dir() / "_internal"


def _read_version(version_json: Path) -> str | None:
    try:
        data = json.loads(version_json.read_text("utf-8"))
        ver = data.get("version")
        return str(ver).lstrip("v") if ver else None
    except Exception:
        return None


# ------------------------------------------------------------------ #
#  バックグラウンドスレッド
# ------------------------------------------------------------------ #

class _ApplyThread(QThread):
    progress = Signal(str)
    finished = Signal()
    error    = Signal(str)

    def run(self) -> None:
        try:
            patch_dir    = _patch_dir()
            internal_dir = _internal_dir()
            lib_src      = patch_dir    / "lib"
            lib_dest     = internal_dir / "lib"
            res_src      = patch_dir    / "resources"
            res_dest     = internal_dir / "resources"
            ver_src      = patch_dir    / "version.json"
            ver_dest     = internal_dir / "version.json"

            # 1. 既存 lib を削除
            self.progress.emit("既存の lib フォルダを削除中...")
            if lib_dest.exists():
                shutil.rmtree(lib_dest)

            # 2. patch/lib を _internal/lib にコピー
            self.progress.emit("新しい lib フォルダをコピー中...")
            shutil.copytree(lib_src, lib_dest)

            # 3. patch/resources を _internal/resources にコピー (存在する場合のみ)
            if res_src.exists():
                self.progress.emit("resources フォルダを更新中...")
                if res_dest.exists():
                    shutil.rmtree(res_dest)
                shutil.copytree(res_src, res_dest)

            # 4. version.json を上書き
            self.progress.emit("version.json を更新中...")
            shutil.copy2(ver_src, ver_dest)

            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


# ------------------------------------------------------------------ #
#  ダイアログ
# ------------------------------------------------------------------ #

class PatchDialog(QDialog):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MyBeatSaberStats パッチ適用")
        self.setMinimumWidth(440)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
        )
        self._thread: _ApplyThread | None = None

        layout = QVBoxLayout(self)

        patch_dir    = _patch_dir()
        internal_dir = _internal_dir()

        # バリデーション
        ok, err_msg = self._validate(patch_dir, internal_dir)
        if not ok:
            layout.addWidget(QLabel(f"<b>エラー:</b><br>{err_msg}"))
            close_btn = QPushButton("閉じる")
            close_btn.clicked.connect(self.reject)
            layout.addWidget(close_btn)
            return

        # バージョン表示
        current_ver = _read_version(internal_dir / "version.json") or "不明"
        new_ver     = _read_version(patch_dir    / "version.json") or "不明"
        layout.addWidget(QLabel(
            f"<b>パッチを適用しますか？</b><br><br>"
            f"現在のバージョン: <b>v{current_ver}</b><br>"
            f"適用後のバージョン: <b>v{new_ver}</b><br><br>"
            f"<small>※ 適用前にアプリを終了してください。</small>"
        ))

        # 進捗表示
        self._progress_label = QLabel("")
        self._progress_bar   = QProgressBar()
        self._progress_bar.setRange(0, 0)
        self._progress_label.hide()
        self._progress_bar.hide()
        layout.addWidget(self._progress_label)
        layout.addWidget(self._progress_bar)

        # ボタン行
        btn_row = QHBoxLayout()
        self._apply_btn  = QPushButton("適用する")
        self._cancel_btn = QPushButton("キャンセル")
        self._apply_btn.setDefault(True)
        self._apply_btn.clicked.connect(self._start_apply)
        self._cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch(1)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._apply_btn)
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------

    @staticmethod
    def _validate(patch_dir: Path, internal_dir: Path) -> tuple[bool, str]:
        if not patch_dir.exists():
            return False, (
                f"patch フォルダが見つかりません。<br>"
                f"apply_patch.exe と同じフォルダに patch/ フォルダを置いてください。<br>"
                f"<small>{patch_dir}</small>"
            )
        if not (patch_dir / "version.json").exists():
            return False, "patch/version.json が見つかりません。"
        if not (patch_dir / "lib").exists():
            return False, "patch/lib フォルダが見つかりません。"
        if not internal_dir.exists():
            return False, (
                f"_internal フォルダが見つかりません。<br>"
                f"メインアプリと同じフォルダで実行してください。<br>"
                f"<small>{internal_dir}</small>"
            )
        if not (internal_dir / "version.json").exists():
            return False, "_internal/version.json が見つかりません。"
        return True, ""

    def _start_apply(self) -> None:
        self._apply_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)
        self._progress_label.show()
        self._progress_bar.show()

        self._thread = _ApplyThread()
        self._thread.progress.connect(self._on_progress)
        self._thread.finished.connect(self._on_finished)
        self._thread.error.connect(self._on_error)
        self._thread.start()

    def _on_progress(self, msg: str) -> None:
        self._progress_label.setText(msg)
        QApplication.processEvents()

    def _on_finished(self) -> None:
        self._progress_bar.setRange(0, 1)
        self._progress_bar.setValue(1)
        self._progress_label.setText("完了しました！")
        new_ver = _read_version(_patch_dir() / "version.json") or "不明"
        QMessageBox.information(
            self,
            "パッチ適用完了",
            f"v{new_ver} へのパッチ適用が完了しました。\n"
            "アプリを起動すると新しいバージョンが有効になります。",
        )
        self.accept()

    def _on_error(self, msg: str) -> None:
        self._progress_bar.setRange(0, 1)
        self._progress_bar.setValue(0)
        self._progress_label.setText("エラーが発生しました。")
        self._apply_btn.setEnabled(True)
        self._cancel_btn.setEnabled(True)
        QMessageBox.critical(self, "エラー", f"パッチ適用中にエラーが発生しました:\n{msg}")


# ------------------------------------------------------------------ #
#  エントリポイント
# ------------------------------------------------------------------ #

def main() -> None:
    app = QApplication(sys.argv)
    dlg = PatchDialog()
    dlg.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
