"""アプリ全体のテーマ（ダーク / ライト）管理モジュール。

使い方:
    from .theme import apply_dark, apply_light, is_dark, table_stylesheet, init_theme

    # 起動時に保存済み設定 or Windows システム設定でテーマを初期化
    init_theme(QApplication.instance())

    # テーブルの QSS を取得
    self.table.setStyleSheet(table_stylesheet())
"""

from __future__ import annotations

import json

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .snapshot import BASE_DIR

# 設定ファイルパス（cache/settings.json に dark_mode キーで保存）
_PREF_PATH = BASE_DIR / "cache" / "settings.json"

# ------------------------------------------------------------------ #
#  内部状態
# ------------------------------------------------------------------ #
_dark_mode: bool = False           # 実際の表示状態 (True=ダーク)
_theme_mode: str = "default"       # 設定モード: "default" | "dark" | "light"


def detect_system_dark() -> bool:
    """Windows のシステムダークモード設定を返す。取得できない場合は False。"""
    try:
        import winreg  # Windows のみ利用可能
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return value == 0  # 0 = ダーク, 1 = ライト
    except Exception:
        return False


def _load_pref() -> str | None:
    """保存済みのテーマモードを返す ("default"/"dark"/"light")。未保存の場合は None。"""
    try:
        if _PREF_PATH.exists():
            data = json.loads(_PREF_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # 新フォーマット
                if "theme_mode" in data:
                    v = data["theme_mode"]
                    if v in ("default", "dark", "light"):
                        return v
                # 旧フォーマット (bool) との互換性
                if "dark_mode" in data:
                    return "dark" if data["dark_mode"] else "light"
    except Exception:
        pass
    return None


def _save_pref(mode: str) -> None:
    """テーマモードを JSON ファイルに保存する。"""
    try:
        _PREF_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if _PREF_PATH.exists():
            try:
                existing = json.loads(_PREF_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        existing["theme_mode"] = mode
        existing.pop("dark_mode", None)  # 旧フォーマットキーを削除
        _PREF_PATH.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def is_dark() -> bool:
    """現在ダークモードが有効かどうかを返す。"""
    return _dark_mode


def current_theme_mode() -> str:
    """現在のテーマモードを返す ("default" | "dark" | "light")。"""
    return _theme_mode


def button_label() -> str:
    """ダークモード切替ボタンに表示すべきラベルを返す。

    - Default 状態: クリックで強制モードに切り替わることを示す
      ダーク表示中 → "☀\ufe0f Light" (クリックで強制ライトへ)
      ライト表示中 → "\U0001f319 Dark"  (クリックで強制ダークへ)
    - 強制モード: クリックで Default に戻ることを示す "\u21a9 Default"
    """
    if _theme_mode == "default":
        return "\u2600\ufe0f Light" if _dark_mode else "\U0001f319 Dark"
    return "\u21a9 Default"


# ------------------------------------------------------------------ #
#  テーブル QSS
# ------------------------------------------------------------------ #
_TABLE_STYLE_LIGHT = (
    "QTableWidget { background-color: #ffffff; alternate-background-color: #f6f7fb; }"
    "QTableWidget::item { padding-top: 0px; padding-bottom: 0px; }"
)

_TABLE_STYLE_DARK = (
    "QTableWidget { background-color: #121212; alternate-background-color: #191919; "
    "color: #d4d4d4; gridline-color: #434343; }"
    "QTableWidget::item { padding-top: 0px; padding-bottom: 0px; }"
)


def table_stylesheet() -> str:
    """現在のテーマに合ったテーブル用スタイルシートを返す。"""
    return _TABLE_STYLE_DARK if _dark_mode else _TABLE_STYLE_LIGHT


_TOGGLE_ON_DARK = (
    "QPushButton { background-color: #2a2a2a; color: #888888; "
    "border: 1px solid #555555; border-radius: 3px; padding: 2px 8px; } "
    "QPushButton:checked { background-color: #1e6423; color: #ffffff; "
    "border: 1px solid #4caf50; font-weight: bold; } "
    "QPushButton:hover:!checked { background-color: #3a3a3a; } "
    "QPushButton:hover:checked { background-color: #2a8530; }"
)
_TOGGLE_ON_LIGHT = (
    "QPushButton { background-color: #cccccc; color: #888888; "
    "border: 1px solid #aaaaaa; border-radius: 3px; padding: 2px 8px; } "
    "QPushButton:checked { background-color: #2e7d32; color: #ffffff; "
    "border: 1px solid #43a047; font-weight: bold; } "
    "QPushButton:hover:!checked { background-color: #bbbbbb; } "
    "QPushButton:hover:checked { background-color: #388e3c; }"
)

# 排他選択（ラジオボタン的）のトグルボタン—青系で緿のトグルと区別する
_RADIO_TOGGLE_DARK = (
    "QPushButton { background-color: #2a2a2a; color: #888888; "
    "border: 1px solid #555555; border-radius: 3px; padding: 2px 8px; } "
    "QPushButton:checked { background-color: #1a4a7a; color: #ffffff; "
    "border: 1px solid #4a9edd; font-weight: bold; } "
    "QPushButton:hover:!checked { background-color: #3a3a3a; } "
    "QPushButton:hover:checked { background-color: #1e5c9a; }"
)
_RADIO_TOGGLE_LIGHT = (
    "QPushButton { background-color: #cccccc; color: #888888; "
    "border: 1px solid #aaaaaa; border-radius: 3px; padding: 2px 8px; } "
    "QPushButton:checked { background-color: #1565c0; color: #ffffff; "
    "border: 1px solid #42a5f5; font-weight: bold; } "
    "QPushButton:hover:!checked { background-color: #bbbbbb; } "
    "QPushButton:hover:checked { background-color: #1976d2; }"
)


def toggle_button_stylesheet() -> str:
    """ON/OFF がわかりやすいトグルボタン用スタイルシートを返す。"""
    return _TOGGLE_ON_DARK if _dark_mode else _TOGGLE_ON_LIGHT


def radio_toggle_stylesheet() -> str:
    """排他的選択（ラジオボタン的）なトグルボタン用スタイルシートを返す。青系で通常トグルと区別する。"""
    return _RADIO_TOGGLE_DARK if _dark_mode else _RADIO_TOGGLE_LIGHT


_ACTION_BUTTON_DARK = (
    "QPushButton { background-color: #1a4a7a; color: #ffffff; "
    "border: 1px solid #4a9edd; border-radius: 3px; padding: 2px 8px; font-weight: bold; } "
    "QPushButton:hover { background-color: #1e5c9a; }"
)
_ACTION_BUTTON_LIGHT = (
    "QPushButton { background-color: #1565c0; color: #ffffff; "
    "border: 1px solid #42a5f5; border-radius: 3px; padding: 2px 8px; font-weight: bold; } "
    "QPushButton:hover { background-color: #1976d2; }"
)


def action_button_stylesheet() -> str:
    """常時アクティブ（ON 色）に見えるアクションボタン用スタイルシートを返す。"""
    return _ACTION_BUTTON_DARK if _dark_mode else _ACTION_BUTTON_LIGHT


# ------------------------------------------------------------------ #
#  テーマ別カラーヘルパー
# ------------------------------------------------------------------ #
def label_cell_color() -> QColor:
    """Metric / ★ などラベル列のセル背景色。"""
    return QColor("#2d2d2d") if _dark_mode else QColor(248, 248, 248)


def label_cell_text_color() -> QColor:
    """ラベル列のテキスト色。"""
    return QColor("#e0e0e0") if _dark_mode else QColor("#111111")


def diff_positive_bg() -> QColor:
    """diff がプラス（改善）のセル背景色。"""
    return QColor("#3a7a3a") if _dark_mode else QColor(180, 255, 180)


def diff_negative_bg() -> QColor:
    """diff がマイナス（悪化）のセル背景色。"""
    return QColor("#7a3a3a") if _dark_mode else QColor(255, 200, 200)


def diff_neutral_bg() -> QColor:
    """diff がゼロ（変化なし）のセル背景色。"""
    return QColor("#2a2a2a") if _dark_mode else QColor(230, 230, 230)


def diff_text_color() -> QColor:
    """diff セルのテキスト色。ダーク時は明るめ、ライト時は暗め。"""
    return QColor("#e0e0e0") if _dark_mode else QColor("#111111")


# ------------------------------------------------------------------ #
#  パレット生成
# ------------------------------------------------------------------ #
def _dark_palette() -> QPalette:
    p = QPalette()

    base        = QColor("#121212")
    alt_base    = QColor("#191919")
    window      = QColor("#252526")
    window_text = QColor("#d4d4d4")
    button      = QColor("#3c3c3c")
    button_text = QColor("#d4d4d4")
    highlight   = QColor("#264f78")
    hi_text     = QColor("#ffffff")
    text        = QColor("#d4d4d4")
    disabled    = QColor("#6b6b6b")
    mid         = QColor("#3c3c3c")
    tooltip_bg  = QColor("#252526")
    tooltip_fg  = QColor("#d4d4d4")
    link        = QColor("#4ec9b0")

    p.setColor(QPalette.ColorRole.Window,          window)
    p.setColor(QPalette.ColorRole.WindowText,      window_text)
    p.setColor(QPalette.ColorRole.Base,            base)
    p.setColor(QPalette.ColorRole.AlternateBase,   alt_base)
    p.setColor(QPalette.ColorRole.ToolTipBase,     tooltip_bg)
    p.setColor(QPalette.ColorRole.ToolTipText,     tooltip_fg)
    p.setColor(QPalette.ColorRole.Text,            text)
    p.setColor(QPalette.ColorRole.Button,          button)
    p.setColor(QPalette.ColorRole.ButtonText,      button_text)
    p.setColor(QPalette.ColorRole.BrightText,      QColor("#ffffff"))
    p.setColor(QPalette.ColorRole.Highlight,       highlight)
    p.setColor(QPalette.ColorRole.HighlightedText, hi_text)
    p.setColor(QPalette.ColorRole.Link,            link)
    p.setColor(QPalette.ColorRole.Mid,             mid)

    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,       disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled)

    return p


_DARK_GLOBAL_QSS = """
QDialog {
    background-color: #141414;
}
QToolTip {
    color: #d4d4d4;
    background-color: #252526;
    border: 1px solid #3c3c3c;
}
QHeaderView::section {
    background-color: #333333;
    color: #e0e0e0;
    border: none;
    border-right: 1px solid #434343;
    border-bottom: 1px solid #434343;
}
QHeaderView::section:vertical {
    background-color: #2d2d2d;
    color: #e0e0e0;
    border: none;
    border-right: 1px solid #434343;
    border-bottom: 1px solid #434343;
    padding-right: 4px;
}
QScrollBar:horizontal, QScrollBar:vertical {
    background: #2d2d2d;
}
QScrollBar::handle:horizontal, QScrollBar::handle:vertical {
    background: #5a5a5a;
    border-radius: 4px;
}
QPushButton {
    background-color: #3a3a3a;
    color: #e0e0e0;
    border: 1px solid #5a5a5a;
    border-radius: 3px;
    padding: 2px 8px;
}
QPushButton:hover {
    background-color: #4a4a4a;
    border-color: #777777;
}
QPushButton:pressed {
    background-color: #2a2a2a;
}
QPushButton:checked {
    background-color: #264f78;
    color: #ffffff;
    border-color: #4a90d9;
}
QPushButton:disabled {
    background-color: #2a2a2a;
    color: #6b6b6b;
    border-color: #3c3c3c;
}
QComboBox {
    background-color: #3c3c3c;
    color: #d4d4d4;
    border: 1px solid #555555;
}
QComboBox QAbstractItemView {
    background-color: #252526;
    color: #d4d4d4;
    selection-background-color: #264f78;
}
QLineEdit, QDateTimeEdit {
    background-color: #3c3c3c;
    color: #d4d4d4;
    border: 1px solid #555555;
}
QGroupBox {
    color: #d4d4d4;
    border: 1px solid #555555;
}
QCheckBox {
    color: #CCAA99;
}
QProgressDialog QLabel {
    color: #d4d4d4;
}
"""


# ------------------------------------------------------------------ #
#  公開 API
# ------------------------------------------------------------------ #

# Windows がライトモードのとき: ネイティブ描画を壊さないよう最小限のみ指定する。
# QCheckBox などを明示すると Qt がネイティブスタイルから独自描画に切り替えてしまう。
_LIGHT_NATIVE_QSS = """
QPushButton {
    padding: 2px 8px;
}
"""

# Windows がダークモードで強制ライトのとき: ネイティブダークスタイルを完全上書きする必要がある。
_LIGHT_FORCED_QSS = """
QToolTip {
    color: #000000;
    background-color: #ffffdc;
    border: 1px solid #aaaaaa;
}
QHeaderView::section {
    background-color: #f0f0f0;
    color: #000000;
    border: none;
    border-right: 1px solid #d0d0d0;
    border-bottom: 1px solid #d0d0d0;
}
QHeaderView::section:vertical {
    background-color: #f8f8f8;
    color: #111111;
    border: none;
    border-right: 1px solid #d0d0d0;
    border-bottom: 1px solid #d0d0d0;
    padding-right: 4px;
}
QScrollBar:horizontal, QScrollBar:vertical {
    background: #f0f0f0;
}
QScrollBar::handle:horizontal, QScrollBar::handle:vertical {
    background: #c0c0c0;
    border-radius: 4px;
}
QPushButton {
    background-color: #e1e1e1;
    color: #000000;
    border: 1px solid #adadad;
    border-radius: 3px;
    padding: 2px 8px;
}
QPushButton:hover {
    background-color: #e5f1fb;
    border-color: #0078d4;
}
QPushButton:pressed {
    background-color: #cce4f7;
}
QPushButton:checked {
    background-color: #cce4f7;
    color: #000000;
    border-color: #0078d4;
}
QPushButton:disabled {
    background-color: #f0f0f0;
    color: #a0a0a0;
    border-color: #d0d0d0;
}
QComboBox {
    background-color: #ffffff;
    color: #000000;
    border: 1px solid #adadad;
}
QComboBox QAbstractItemView {
    background-color: #ffffff;
    color: #000000;
    selection-background-color: #0078d4;
    selection-color: #ffffff;
}
QLineEdit, QDateTimeEdit {
    background-color: #ffffff;
    color: #000000;
    border: 1px solid #adadad;
}
QGroupBox {
    color: #000000;
    border: 1px solid #adadad;
}

QCheckBox {
    color: #003580;
}
QProgressDialog QLabel {
    color: #000000;
}
"""


def _light_palette() -> QPalette:
    """明示的なライトパレットを生成する。

    Windows ダークモード時に QPalette() がシステムのダークパレットを返してしまう問題を
    回避するため、白ベースの色を明示的に指定する。
    """
    p = QPalette()
    white      = QColor("#ffffff")
    light_gray = QColor("#f0f0f0")
    mid_gray   = QColor("#c0c0c0")
    dark_text  = QColor("#000000")
    window     = QColor("#f0f0f0")
    highlight  = QColor("#0078d4")
    hi_text    = QColor("#ffffff")
    link       = QColor("#0066cc")
    disabled   = QColor("#a0a0a0")

    p.setColor(QPalette.ColorRole.Window,          window)
    p.setColor(QPalette.ColorRole.WindowText,      dark_text)
    p.setColor(QPalette.ColorRole.Base,            white)
    p.setColor(QPalette.ColorRole.AlternateBase,   light_gray)
    p.setColor(QPalette.ColorRole.ToolTipBase,     white)
    p.setColor(QPalette.ColorRole.ToolTipText,     dark_text)
    p.setColor(QPalette.ColorRole.Text,            dark_text)
    p.setColor(QPalette.ColorRole.Button,          window)
    p.setColor(QPalette.ColorRole.ButtonText,      dark_text)
    p.setColor(QPalette.ColorRole.BrightText,      QColor("#ff0000"))
    p.setColor(QPalette.ColorRole.Highlight,       highlight)
    p.setColor(QPalette.ColorRole.HighlightedText, hi_text)
    p.setColor(QPalette.ColorRole.Link,            link)
    p.setColor(QPalette.ColorRole.Mid,             mid_gray)

    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,       disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled)

    return p


def apply_dark(app: QApplication | None = None) -> None:
    """アプリ全体にダークテーマを適用する。"""
    global _dark_mode
    _dark_mode = True
    a: QApplication | None = app or QApplication.instance()  # type: ignore[assignment]
    if a:
        a.setPalette(_dark_palette())
        a.setStyleSheet(_DARK_GLOBAL_QSS)


def apply_light(app: QApplication | None = None) -> None:
    """アプリ全体をライトテーマに戻す。

    - Windows がライトモードのとき: QPalette() + 最小限QSS でネイティブ描画を維持する。
      QCheckBox などを QSS で明示するとネイティブスタイルが無効化されるため使わない。
    - Windows がダークモードのとき: 明示的なパレット + 完全QSS でダーク描画を上書きする。
    """
    global _dark_mode
    _dark_mode = False
    a: QApplication | None = app or QApplication.instance()  # type: ignore[assignment]
    if a:
        if detect_system_dark():
            # システムがダーク → 強制ライトパレット + 完全QSSで上書き
            a.setPalette(_light_palette())
            a.setStyleSheet(_LIGHT_FORCED_QSS)
        else:
            # システムがライト → ネイティブパレット + 最小限QSS (ネイティブ描画を維持)
            a.setPalette(QPalette())
            a.setStyleSheet(_LIGHT_NATIVE_QSS)


def toggle(app: QApplication | None = None) -> bool:
    """テーマを切り替えて、切り替え後のダーク状態 (True=dark) を返す。

    Default 状態のとき: 現在の表示の反対を強制モードとして適用
    強制モードのとき : Default に戻り、Windows システム設定を再適用
    """
    global _theme_mode
    if _theme_mode == "default":
        # 現在の表示の逆を強制モードとして設定
        if _dark_mode:
            _theme_mode = "light"
            apply_light(app)
        else:
            _theme_mode = "dark"
            apply_dark(app)
    else:
        # Default に戻す → Windows システム設定を再適用
        _theme_mode = "default"
        if detect_system_dark():
            apply_dark(app)
        else:
            apply_light(app)
    _save_pref(_theme_mode)
    return _dark_mode


def init_theme(app: QApplication | None = None) -> None:
    """起動時のテーマを決定して適用する。

    保存済みの設定があればそれを使用し、なければ Default (Windows システム設定) を使用する。
    """
    global _theme_mode
    mode = _load_pref() or "default"
    _theme_mode = mode
    if mode == "dark":
        apply_dark(app)
    elif mode == "light":
        apply_light(app)
    else:  # "default"
        if detect_system_dark():
            apply_dark(app)
        else:
            apply_light(app)
