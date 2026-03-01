"""アプリ全体のテーマ（ダーク / ライト）管理モジュール。

使い方:
    from .theme import apply_dark, apply_light, is_dark, table_stylesheet

    # ダークモードを適用
    apply_dark(QApplication.instance())

    # テーブルの QSS を取得
    self.table.setStyleSheet(table_stylesheet())
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

# ------------------------------------------------------------------ #
#  内部状態
# ------------------------------------------------------------------ #
_dark_mode: bool = False


def is_dark() -> bool:
    """現在ダークモードが有効かどうかを返す。"""
    return _dark_mode


# ------------------------------------------------------------------ #
#  テーブル QSS
# ------------------------------------------------------------------ #
_TABLE_STYLE_LIGHT = (
    "QTableWidget { background-color: #ffffff; alternate-background-color: #f6f7fb; }"
)
_TABLE_STYLE_DARK = (
    "QTableWidget { background-color: #1e1e1e; alternate-background-color: #2a2a2a; "
    "color: #d4d4d4; gridline-color: #3c3c3c; }"
)


def table_stylesheet() -> str:
    """現在のテーマに合ったテーブル用スタイルシートを返す。"""
    return _TABLE_STYLE_DARK if _dark_mode else _TABLE_STYLE_LIGHT


# ------------------------------------------------------------------ #
#  テーマ別カラーヘルパー
# ------------------------------------------------------------------ #
def label_cell_color() -> QColor:
    """Metric / ★ などラベル列のセル背景色。"""
    return QColor("#2d2d2d") if _dark_mode else QColor(248, 248, 248)


def label_cell_text_color() -> QColor:
    """ラベル列のテキスト色。"""
    return QColor("#cccccc") if _dark_mode else QColor("#111111")


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

    base        = QColor("#1e1e1e")
    alt_base    = QColor("#2a2a2a")
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
QToolTip {
    color: #d4d4d4;
    background-color: #252526;
    border: 1px solid #3c3c3c;
}
QHeaderView::section {
    background-color: #2d2d2d;
    color: #cccccc;
    border: none;
    border-right: 1px solid #3c3c3c;
    border-bottom: 1px solid #3c3c3c;
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
    color: #d4d4d4;
}
QProgressDialog QLabel {
    color: #d4d4d4;
}
"""


# ------------------------------------------------------------------ #
#  公開 API
# ------------------------------------------------------------------ #

_LIGHT_GLOBAL_QSS = """
QPushButton {
    padding: 2px 6px;
}
"""


def apply_dark(app: QApplication | None = None) -> None:
    """アプリ全体にダークテーマを適用する。"""
    global _dark_mode
    _dark_mode = True
    a: QApplication | None = app or QApplication.instance()  # type: ignore[assignment]
    if a:
        a.setPalette(_dark_palette())
        a.setStyleSheet(_DARK_GLOBAL_QSS)


def apply_light(app: QApplication | None = None) -> None:
    """アプリ全体をシステムデフォルト（ライト）テーマに戻す。"""
    global _dark_mode
    _dark_mode = False
    a: QApplication | None = app or QApplication.instance()  # type: ignore[assignment]
    if a:
        a.setPalette(QPalette())   # デフォルトパレットに戻す
        a.setStyleSheet(_LIGHT_GLOBAL_QSS)


def toggle(app: QApplication | None = None) -> bool:
    """ダーク/ライトを切り替えて、切り替え後の状態 (True=dark) を返す。"""
    if _dark_mode:
        apply_light(app)
    else:
        apply_dark(app)
    return _dark_mode
