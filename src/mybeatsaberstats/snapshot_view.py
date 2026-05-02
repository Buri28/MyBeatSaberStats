from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from datetime import datetime, timezone
import json
import re
from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QColor, QIcon, QPalette
from .theme import (
    detect_system_dark,
    label_cell_color,
    label_cell_text_color,
    diff_positive_bg,
    diff_negative_bg,
    diff_neutral_bg,
    diff_text_color,
    table_stylesheet,
    toggle_button_stylesheet,
    radio_toggle_stylesheet,
    action_button_stylesheet,
    action_button_red_stylesheet,
    action_button_green_stylesheet,
    is_dark,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
    QSizePolicy,
    QSplitter,
    QStyledItemDelegate,
)

from .snapshot import Snapshot, SNAPSHOT_DIR, BASE_DIR, RESOURCES_DIR
from .accsaber import get_accsaber_playlist_map_counts_from_cache
from .accsaber_reloaded import get_reloaded_map_counts_from_cache as _get_reloaded_map_counts_from_cache


def _light_app_button_min_height() -> int:
    if is_dark():
        return 0
    return 20 if detect_system_dark() else 23


class PercentageBarDelegate(QStyledItemDelegate):
    """パーセンテージ値を持つセルに簡易な横棒グラフを描画するデリゲート。

    テキスト色はダーク/ライト × バー重なりあり/なし の4パターンを個別に指定可能。
    None を指定するとパレットのデフォルト色をそのまま使用する。
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        max_value: float = 100.0,
        gradient_min: float = 0.0,
        dark_text_on_bar: Optional[str] = "#3333FF",
        dark_text_off_bar: Optional[str] = "#3388FF",
        light_text_on_bar: Optional[str] = "#2222FF",
        light_text_off_bar: Optional[str] = "#111199",
    ) -> None:
        super().__init__(parent)
        self._max_value = max_value
        self._min_value = gradient_min
        self._dark_text_on_bar = dark_text_on_bar
        self._dark_text_off_bar = dark_text_off_bar
        self._light_text_on_bar = light_text_on_bar
        self._light_text_off_bar = light_text_off_bar

    def _parse_value(self, value_str) -> "Optional[float]":
        if value_str in (None, ""):
            return None
        s = str(value_str).strip()
        m = re.search(r"([-+]?\d+(?:\.\d+)?)\s*%", s)
        num_str = m.group(1) if m else s
        try:
            return float(num_str)
        except ValueError:
            return None

    def _resolve_value(self, index) -> "Optional[float]":
        """表示テキストまたは UserRole から数値を解決する。"""
        value = self._parse_value(index.data())
        if value is None:
            user_val = index.data(Qt.ItemDataRole.UserRole)
            if isinstance(user_val, (int, float)):
                value = float(user_val)
        return value

    def _compute_bar_rgb(self, value: float) -> "tuple[float, int, int, int]":
        """value からバー比率と RGB を返す。"""
        if value <= self._min_value:
            ratio = 0.0
        else:
            span = self._max_value - self._min_value
            ratio = (value - self._min_value) / span if span > 0 else 0.0
        ratio = max(0.0, min(1.0, ratio))

        if ratio <= 0.5:
            t = ratio / 0.5 if ratio > 0 else 0.0
            r, g, b = 255, int(255 * t), 0
        elif ratio <= 0.8:
            t = (ratio - 0.5) / 0.3
            r, g, b = int(255 * (1.0 - t)), 255, 0
        else:
            t = (ratio - 0.8) / 0.2
            r, g, b = 0, 255, int(255 * t / 2)
        return ratio, r, g, b

    def initStyleOption(self, option, index) -> None:  # type: ignore[override]
        super().initStyleOption(option, index)
        value = self._resolve_value(index)
        if value is None or not (self._max_value > 0):
            return

        ratio, r, g, b = self._compute_bar_rgb(value)

        if value >= self._max_value - 1e-3:
            option.font.setBold(True)

        # バー輝度に応じてテキスト色を決定（super() が setForeground 色で上書いた後に再設定）
        bar_lum = 0.299 * r + 0.587 * g + 0.114 * b
        # バーが薄いときはテキストを濃く、バーが濃いときはテキストを薄くする。閾値は30%のバー重なりで切り替える。
        use_dark_text = ratio >= 0.3 and bar_lum > 140
        dark = is_dark()
        text_color_str = (
            self._dark_text_on_bar if dark else self._light_text_on_bar
        ) if use_dark_text else (
            self._dark_text_off_bar if dark else self._light_text_off_bar
        )
        if text_color_str is not None:
            pal = option.palette
            pal.setColor(QPalette.ColorRole.Text, QColor(text_color_str))
            option.palette = pal

    def paint(self, painter, option, index):  # type: ignore[override]
        value = self._resolve_value(index)

        if value is None or not (self._max_value > 0):
            return super().paint(painter, option, index)

        ratio, r, g, b = self._compute_bar_rgb(value)

        painter.save()
        rect = option.rect.adjusted(1, 1, -1, -1)
        bar_width = int(rect.width() * ratio)
        bar_rect = rect.adjusted(0, 0, bar_width - rect.width(), 0)

        color = QColor(r, g, b if ratio > 0.8 else 0, 180)
        painter.fillRect(bar_rect, color)
        painter.restore()

        super().paint(painter, option, index)


# AccSaber Play Count バーの共通色定義（stats 画面・比較画面で共有）
# Overall=紫, True=緑, Standard=青, Tech=赤
ACC_PLAY_COLORS: dict[str, QColor] = {
    "overall":  QColor(138,  60, 224, 180),
    "true":     QColor( 26, 234, 133, 160),
    "standard": QColor( 41, 128, 255, 180),
    "tech":     QColor(255,  74,  74, 180),
}
# stats 画面の列インデックス → カテゴリ名
ACC_PLAY_COL_CATS: dict[int, str] = {1: "overall", 2: "true", 3: "standard", 4: "tech"}


class AccPlayCountBarDelegate(QStyledItemDelegate):
    """AccSaber / AccSaber Reloaded の Play Count セルにバーを描画するデリゲート。

    stats 画面: 列インデックスを ACC_PLAY_COL_CATS で解決して色を決定する。
    比較画面: UserRole+1 に QColor が設定されている場合はそちらを優先する。
    どちらも UserRole に割合 (0.0〜1.0) が設定されているセルのみバーを描画。
    """

    @staticmethod
    def _gradient_rgb(ratio: float) -> "tuple[int, int, int]":
        """ratio (0.0–1.0) から赤→黄→緑グラデーションの RGB を返す。"""
        if ratio <= 0.5:
            t = ratio / 0.5 if ratio > 0 else 0.0
            return 255, int(255 * t), 0
        elif ratio <= 0.8:
            t = (ratio - 0.5) / 0.3
            return int(255 * (1.0 - t)), 255, 0
        else:
            t = (ratio - 0.8) / 0.2
            return 0, 255, int(255 * t / 2)

    def _resolve_color(self, ratio: float, color_raw) -> QColor:
        """UserRole+1 の値からバー色を決定する。"""
        if isinstance(color_raw, QColor):
            return color_raw
        elif color_raw is True:
            r, g, b = self._gradient_rgb(ratio)
            return QColor(r, g, b if ratio > 0.8 else 0, 180)
        else:
            cat = ACC_PLAY_COL_CATS.get(-1)  # 列インデックス不明時のフォールバック
            return QColor(128, 128, 128, 160)

    def _resolve_color_for_index(self, ratio: float, index) -> QColor:
        color_raw = index.data(Qt.ItemDataRole.UserRole + 1)
        if isinstance(color_raw, QColor):
            return color_raw
        elif color_raw is True:
            r, g, b = self._gradient_rgb(ratio)
            return QColor(r, g, b if ratio > 0.8 else 0, 180)
        else:
            cat = ACC_PLAY_COL_CATS.get(index.column())
            return ACC_PLAY_COLORS.get(cat, QColor(128, 128, 128, 160)) if cat else QColor(128, 128, 128, 160)

    def initStyleOption(self, option, index) -> None:  # type: ignore[override]
        super().initStyleOption(option, index)
        ratio_raw = index.data(Qt.ItemDataRole.UserRole)
        if ratio_raw is None:
            return
        try:
            ratio = max(0.0, min(1.0, float(ratio_raw)))
        except (TypeError, ValueError):
            return

        color_raw = index.data(Qt.ItemDataRole.UserRole + 1)
        if color_raw is not True:
            return  # 固定色バー（Play Count）はテキスト色を変えない

        # Avg Acc グラデーション: バー輝度に応じてテキスト色を設定
        r, g, b = self._gradient_rgb(ratio)
        bar_lum = 0.299 * r + 0.587 * g + 0.114 * b
        use_dark_text = ratio >= 0.3 and bar_lum > 140
        dark = is_dark()
        text_color_str = (
            "#3333FF" if dark else "#2222FF"
        ) if use_dark_text else (
            "#3388FF" if dark else "#111199"
        )
        pal = option.palette
        pal.setColor(QPalette.ColorRole.Text, QColor(text_color_str))
        option.palette = pal

    def paint(self, painter, option, index):  # type: ignore[override]
        ratio_raw = index.data(Qt.ItemDataRole.UserRole)
        if ratio_raw is None:
            return super().paint(painter, option, index)
        try:
            ratio = max(0.0, min(1.0, float(ratio_raw)))
        except (TypeError, ValueError):
            return super().paint(painter, option, index)

        color = self._resolve_color_for_index(ratio, index)

        painter.save()
        rect = option.rect.adjusted(1, 1, -1, -1)
        bar_width = int(rect.width() * ratio)
        if bar_width > 0:
            bar_rect = rect.adjusted(0, 0, bar_width - rect.width(), 0)
            painter.fillRect(bar_rect, color)
        painter.restore()
        super().paint(painter, option, index)


class ColumnMaxBarDelegate(QStyledItemDelegate):
    """Compare 画面の PP 列用: 列内最大値を MAX として青色の横棒グラフを描画する。"""

    def _parse_value(self, text) -> Optional[float]:
        try:
            if text in (None, ""):
                return None
            return float(str(text).replace(",", ""))
        except (ValueError, TypeError):
            return None

    def _col_max(self, index) -> Optional[float]:
        model = index.model()
        if model is None:
            return None
        col = index.column()
        last_row = model.rowCount() - 1
        max_val: Optional[float] = None
        for row in range(model.rowCount()):
            if row == last_row:  # Total行は除外
                continue
            v = self._parse_value(model.data(model.index(row, col)))
            if v is not None and (max_val is None or v > max_val):
                max_val = v
        return max_val

    def paint(self, painter, option, index):  # type: ignore[override]
        model = index.model()
        if model is not None and index.row() == model.rowCount() - 1:
            return super().paint(painter, option, index)
        value = self._parse_value(index.data())
        col_max = self._col_max(index)
        if value is None or col_max is None or col_max <= 0:
            return super().paint(painter, option, index)
        ratio = max(0.0, min(1.0, value / col_max))
        painter.save()
        rect = option.rect.adjusted(1, 1, -1, -1)
        bar_width = int(rect.width() * ratio)
        bar_rect = rect.adjusted(0, 0, bar_width - rect.width(), 0)
        painter.fillRect(bar_rect, QColor(0, 160, 255, 160))
        painter.restore()
        super().paint(painter, option, index)


class SnapshotCompareDialog(QDialog):
    """2つのスナップショットを選んで、主要指標の差分を一覧表示するダイアログ。"""

    def __init__(
        self,
        parent: Optional[QWidget] = None,  # type: ignore[name-defined]
        initial_steam_id: Optional[str] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Snapshot Compare")
        # 最大化・最小化ボタンを有効にする
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
        )
        self._default_window_size = (1533, 870)
        self.resize(*self._default_window_size)

        # steam_id ごとにスナップショットを管理する
        self._snapshots_by_player: dict[str, List[Snapshot]] = {}
        # Stats 画面側から渡された「最初に選択しておきたいプレイヤー」
        self._initial_steam_id: Optional[str] = initial_steam_id
        # Metric 列の幅の非表示から復帰用(ここで左端の Metric 列の幅を固定値で保持しておく)
        self._metric_preferred_width: int = 425
        # 全テーブル共通の行高（▲▼ボタンで変更）
        self._row_height: int = 21
        self._ui_state_restored: bool = False
        # スプリッター位置の保存用（_rebalance_splitter と共用）
        self._saved_splitter_sizes: list[int] = [440, 1045]      # _splitter (Metric / right)
        self._saved_right_vsplitter_sizes: list[int] = [615, 190]  # _right_vsplitter (star / acc)
        self._saved_star_hsplitter_ss: int = 392                  # _star_hsplitter SS 側幅
        self._saved_metric_vsplitter_sizes: list[int] = [367, 436]  # _metric_vsplitter (SS/BL / AccSaber)

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(2, 2, 2, 2)
        root_layout.setSpacing(0)

        # サービス別アイコン
        resources_dir = RESOURCES_DIR
        self._icon_scoresaber = QIcon(str(resources_dir / "scoresaber_logo.svg"))
        self._icon_beatleader = QIcon(str(resources_dir / "beatleader_logo.webp"))
        self._icon_accsaber = QIcon(str(resources_dir / "asssaber_logo.webp"))
        _rl_logo_path = resources_dir / "accsaberreloaded_logo.png"
        self._icon_accsaber_rl = QIcon(str(_rl_logo_path)) if _rl_logo_path.exists() else self._icon_accsaber

        # 上部: 左右プレイヤー選択 + それぞれのスナップショット日時選択
        top_grid = QGridLayout()
        top_grid.setAlignment(Qt.AlignmentFlag.AlignLeft)
        top_grid.setContentsMargins(0, 2, 0, 2)
        top_grid.setHorizontalSpacing(5)
        top_grid.setVerticalSpacing(3)

        # 1 行目: Player A / Player B
        top_grid.addWidget(QLabel("Player A:"), 0, 0)
        self.combo_player_a = QComboBox(self)
        top_grid.addWidget(self.combo_player_a, 0, 1)
        top_grid.addWidget(QLabel("　　"), 0, 2)  # スペーサ
        top_grid.addWidget(QLabel("Player B:"), 0, 3)
        self.combo_player_b = QComboBox(self)
        top_grid.addWidget(self.combo_player_b, 0, 4)

        # 2 行目: Snapshot A / Snapshot B
        top_grid.addWidget(QLabel("Snapshot A:"), 1, 0)
        self.combo_a = QComboBox(self)
        # プルダウンが横に伸びすぎないように最大幅を制限する
        self.combo_a.setMaximumWidth(260)
        top_grid.addWidget(self.combo_a, 1, 1)
        top_grid.addWidget(QLabel("　　"), 1, 2)  # スペーサ
        top_grid.addWidget(QLabel("Snapshot B:"), 1, 3)
        self.combo_b = QComboBox(self)
        self.combo_b.setMaximumWidth(260)
        top_grid.addWidget(self.combo_b, 1, 4)

        # Snapshot B 用の「最新を選択」ボタン（B 側の直後に配置）
        self.button_latest_b = QPushButton("Latest", self)
        self.button_latest_b.setFixedWidth(80)
        self.button_latest_b.setToolTip("Select latest snapshot for B")
        top_grid.addWidget(self.button_latest_b, 1, 5)

        # Metric / SS / BL の表示切り替えトグルボタン（左から Metric, SS, BL の順）
        top_grid.addWidget(QLabel("　"), 0, 6)  # スペーサ
        self.btn_toggle_metric = QPushButton("Metric", self)
        self.btn_toggle_metric.setCheckable(True)
        self.btn_toggle_metric.setChecked(True)
        self.btn_toggle_metric.setFixedWidth(55)
        self.btn_toggle_metric.setToolTip("Metric 列の表示/非表示")
        self.btn_toggle_metric.setStyleSheet(toggle_button_stylesheet())
        top_grid.addWidget(self.btn_toggle_metric, 0, 7)

        self.btn_toggle_ss = QPushButton("ScoreSaber", self)
        self.btn_toggle_ss.setCheckable(True)
        self.btn_toggle_ss.setChecked(True)
        self.btn_toggle_ss.setFixedWidth(100)
        self.btn_toggle_ss.setToolTip("ScoreSaber 列の表示/非表示")
        self.btn_toggle_ss.setIcon(self._icon_scoresaber)
        self.btn_toggle_ss.setIconSize(QSize(14, 14))
        self.btn_toggle_ss.setStyleSheet(toggle_button_stylesheet())
        top_grid.addWidget(self.btn_toggle_ss, 0, 8)

        self.btn_toggle_bl = QPushButton("BeatLeader", self)
        self.btn_toggle_bl.setCheckable(True)
        self.btn_toggle_bl.setChecked(True)
        self.btn_toggle_bl.setFixedWidth(100)
        self.btn_toggle_bl.setToolTip("BeatLeader 列の表示/非表示")
        self.btn_toggle_bl.setIcon(self._icon_beatleader)
        self.btn_toggle_bl.setIconSize(QSize(14, 14))
        self.btn_toggle_bl.setStyleSheet(toggle_button_stylesheet())
        top_grid.addWidget(self.btn_toggle_bl, 0, 9)

        self.btn_toggle_header = QPushButton("Header", self)
        self.btn_toggle_header.setCheckable(True)
        self.btn_toggle_header.setChecked(False)  # デフォルト非表示
        self.btn_toggle_header.setFixedWidth(65)
        self.btn_toggle_header.setToolTip("各テーブルのタイトルヘッダの表示/非表示")
        self.btn_toggle_header.setStyleSheet(toggle_button_stylesheet())

        self.btn_bl_below = QPushButton("BL⇨", self)
        self.btn_bl_below.setCheckable(False)
        self.btn_bl_below.setFixedWidth(45)
        self.btn_bl_below.setToolTip("BeatLeaderをScoreSaberの下に配置する / 左右並びに戻す")
        self.btn_bl_below.setStyleSheet(action_button_green_stylesheet())
        top_grid.addWidget(self.btn_bl_below, 0, 10)

        top_grid.addWidget(QLabel("  "), 0, 10 + 1)  # BL と AccSaber の間のスペーサ

        # AccSaber モード切り替えボタン (AccSaber / AccSaber Reloaded) — Metric/SS/BL の右隣
        self._acc_mode: str = "RL"  # "AS" or "RL"
        self._acc_position: str = "Left"  # "Left" or "Bottom"
        self._bl_below: bool = True
        self.btn_acc_as = QPushButton("AccSaber", self)
        self.btn_acc_as.setCheckable(True)
        self.btn_acc_as.setChecked(False)
        self.btn_acc_as.setFixedWidth(100)
        self.btn_acc_as.setToolTip("AccSaberを表示")
        self.btn_acc_as.setIcon(self._icon_accsaber)
        self.btn_acc_as.setIconSize(QSize(14, 14))
        self.btn_acc_as.setStyleSheet(radio_toggle_stylesheet())
        top_grid.addWidget(self.btn_acc_as, 0, 12)

        self.btn_acc_rl = QPushButton("AccSaber RL", self)
        self.btn_acc_rl.setCheckable(True)
        self.btn_acc_rl.setChecked(True)
        self.btn_acc_rl.setFixedWidth(100)
        self.btn_acc_rl.setToolTip("AccSaber Reloadedを表示")
        self.btn_acc_rl.setIcon(self._icon_accsaber_rl)
        self.btn_acc_rl.setIconSize(QSize(14, 14))
        self.btn_acc_rl.setStyleSheet(radio_toggle_stylesheet())
        top_grid.addWidget(self.btn_acc_rl, 0, 13)

        # AccSaber 表示位置切り替えボタン (Left ↔ Bottom トグル)
        self.btn_acc_pos = QPushButton("Acc⇩", self)
        self.btn_acc_pos.setCheckable(False)
        self.btn_acc_pos.setFixedWidth(60)
        self.btn_acc_pos.setToolTip("AccSaberの表示位置を左(⇦)/下(⇩)で切り替える")
        self.btn_acc_pos.setStyleSheet(action_button_stylesheet())
        top_grid.addWidget(self.btn_acc_pos, 0, 14)
        _chk_container = QWidget(self)
        _chk_layout = QHBoxLayout(_chk_container)
        _chk_layout.setContentsMargins(0, 0, 0, 0)
        _chk_layout.setSpacing(3)
        self.chk_col_clear = QCheckBox("Clear", _chk_container)
        self.chk_col_clear.setChecked(True)
        self.chk_col_fc = QCheckBox("FC", _chk_container)
        self.chk_col_fc.setChecked(True)
        self.chk_col_acc = QCheckBox("Acc", _chk_container)
        self.chk_col_acc.setChecked(True)
        self.chk_col_pp = QCheckBox("PP", _chk_container)
        self.chk_col_pp.setChecked(True)
        self.chk_col_starpp = QCheckBox("SPP", _chk_container)
        self.chk_col_starpp.setChecked(True)
        for _chk in (self.chk_col_clear, self.chk_col_fc, self.chk_col_acc, self.chk_col_pp, self.chk_col_starpp):
            _chk_layout.addWidget(_chk)
        # 全選択 / 全解除ボタン
        self.btn_chk_all = QPushButton("All", _chk_container)
        self.btn_chk_all.setFixedWidth(60)
        self.btn_chk_all.setToolTip("全チェック")
        self.btn_chk_all.clicked.connect(self._on_chk_all)
        self.btn_chk_none = QPushButton("None", _chk_container)
        self.btn_chk_none.setFixedWidth(60)
        self.btn_chk_none.setToolTip("全解除")
        self.btn_chk_none.clicked.connect(self._on_chk_none)
        _chk_layout.addWidget(self.btn_chk_all)
        _chk_layout.addWidget(self.btn_chk_none)
        _chk_layout.addStretch(1)
        top_grid.addWidget(_chk_container, 1, 7, 1, 7)

        # ストレッチ列でHeaderボタンを右端に寄せる
        top_grid.setColumnStretch(15, 1)
        top_grid.addWidget(self.btn_toggle_header, 1, 16)

        # 行高 ▲▼ ボタン
        self.btn_row_height_up = QPushButton("▲", self)
        self.btn_row_height_up.setFixedWidth(28)
        self.btn_row_height_up.setToolTip("行の高さを大きくする")
        self.btn_row_height_dn = QPushButton("▼", self)
        self.btn_row_height_dn.setFixedWidth(28)
        self.btn_row_height_dn.setToolTip("行の高さを小さくする")
        top_grid.addWidget(self.btn_row_height_up, 1, 17)
        top_grid.addWidget(self.btn_row_height_dn, 1, 18)

        self.btn_default_layout = QPushButton("Default Layout", self)
        self.btn_default_layout.setToolTip("レイアウトをデフォルトにリセットする")
        top_grid.addWidget(self.btn_default_layout, 1, 19)

        self._plain_header_buttons = [
            self.button_latest_b,
            self.btn_chk_all,
            self.btn_chk_none,
            self.btn_row_height_up,
            self.btn_row_height_dn,
            self.btn_default_layout,
        ]
        self._top_control_buttons = [
            self.button_latest_b,
            self.btn_toggle_metric,
            self.btn_toggle_ss,
            self.btn_toggle_bl,
            self.btn_toggle_header,
            self.btn_bl_below,
            self.btn_acc_as,
            self.btn_acc_rl,
            self.btn_acc_pos,
            self.btn_chk_all,
            self.btn_chk_none,
            self.btn_row_height_up,
            self.btn_row_height_dn,
            self.btn_default_layout,
        ]
        self._apply_header_button_density()
        self._apply_plain_header_button_style()

        # top_grid を QWidget でラップして縦スプリッターの上パネルにする
        _top_ctrl_widget = QWidget(self)
        _top_ctrl_inner = QVBoxLayout(_top_ctrl_widget)
        _top_ctrl_inner.setContentsMargins(0, 0, 0, 0)
        _top_ctrl_inner.setSpacing(0)
        _top_ctrl_inner.addLayout(top_grid)
        _top_ctrl_widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        # 縦スプリッター: 上=コントロールエリア（折り畳み可）、下=テーブルエリア
        self._v_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self._v_splitter.addWidget(_top_ctrl_widget)
        # テーブルエリアは後で addWidget する

        root_layout.addWidget(self._v_splitter, 1)

        # 下部: 左右3つの比較テーブル（上段系 / ScoreSaber★別 / BeatLeader★別）
        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # 左: ScoreSaber/BeatLeader + AccSaber 指標（縦スプリッターで上下に分割）
        self._metric_cmp_container = QWidget()
        _metric_cmp_vbox = QVBoxLayout(self._metric_cmp_container)
        _metric_cmp_vbox.setContentsMargins(0, 0, 0, 0)
        _metric_cmp_vbox.setSpacing(0)
        self._splitter.addWidget(self._metric_cmp_container)

        # 縦スプリッター (上: SS/BL テーブル, 下: AccSaber テーブル)
        self._metric_vsplitter = QSplitter(Qt.Orientation.Vertical, self._metric_cmp_container)

        # 上段: SS/BL テーブル
        _ss_bl_widget = QWidget(self._metric_cmp_container)
        _ss_bl_vbox = QVBoxLayout(_ss_bl_widget)
        _ss_bl_vbox.setContentsMargins(0, 0, 0, 0)
        _ss_bl_vbox.setSpacing(2)
        _metric_hdr_row = QHBoxLayout()
        _metric_hdr_row.setContentsMargins(0, 0, 0, 0)
        _metric_hdr_row.setSpacing(4)
        self._metric_title_label = QLabel("", _ss_bl_widget)
        self._metric_title_label.setStyleSheet("font-size: 11px; padding: 1px 2px;")
        self._metric_title_label.setVisible(False)  # デフォルト非表示
        _metric_hdr_row.addWidget(self._metric_title_label, 1)
        _ss_bl_vbox.addLayout(_metric_hdr_row)

        self.table = QTableWidget(0, 5, _ss_bl_widget)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setStyleSheet(table_stylesheet())
        self.table.verticalHeader().setDefaultSectionSize(14)  # 行の高さを少し詰める
        self.table.verticalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.verticalHeader().setMinimumSectionSize(0)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        # Metric列で内容が分かるため行番号は非表示にする
        self.table.verticalHeader().setVisible(False)

        self.table.setHorizontalHeaderLabels([
            "Genre",
            "Metric",
            "A",
            "B",
            "Diff(A⇒B)",
        ])

        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 96)
        header.resizeSection(1, 150)
        header.resizeSection(2, 85)
        header.resizeSection(3, 85)
        header.resizeSection(4, 95)
        _ss_bl_vbox.addWidget(self.table)
        self._metric_vsplitter.addWidget(_ss_bl_widget)

        # 下段: AccSaber テーブル（ヘッダラベル＋テーブル）
        self._acc_metric_container = QWidget(self._metric_cmp_container)
        _acc_metric_vbox = QVBoxLayout(self._acc_metric_container)
        _acc_metric_vbox.setContentsMargins(0, 0, 0, 0)
        _acc_metric_vbox.setSpacing(2)
        self._acc_metric_title_label = QLabel("", self._acc_metric_container)
        self._acc_metric_title_label.setStyleSheet("font-size: 11px; padding: 1px 2px;")
        self._acc_metric_title_label.setOpenExternalLinks(True)
        _acc_metric_vbox.addWidget(self._acc_metric_title_label)

        self.table_acc = QTableWidget(0, 5, self._acc_metric_container)
        self.table_acc.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table_acc.setStyleSheet(table_stylesheet())
        self.table_acc.verticalHeader().setDefaultSectionSize(14)
        self.table_acc.verticalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table_acc.verticalHeader().setMinimumSectionSize(0)
        self.table_acc.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.table_acc.verticalHeader().setVisible(False)
        self.table_acc.setHorizontalHeaderLabels(["Genre", "Metric", "A", "B", "Diff(A⇒B)"])
        header_acc = self.table_acc.horizontalHeader()
        header_acc.setStretchLastSection(False)
        header_acc.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header_acc.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_acc.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header_acc.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header_acc.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        header_acc.resizeSection(0, 96)
        header_acc.resizeSection(1, 150)
        header_acc.resizeSection(2, 85)
        header_acc.resizeSection(3, 85)
        header_acc.resizeSection(4, 95)
        _acc_metric_vbox.addWidget(self.table_acc)
        self._metric_vsplitter.addWidget(self._acc_metric_container)

        _metric_cmp_vbox.addWidget(self._metric_vsplitter)

        # 右側: 縦スプリッター（上=★別テーブル、下=AccSaberグリッド）
        self._right_vsplitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.addWidget(self._right_vsplitter)

        # 右上: SS/BL ★別テーブル（横並び）
        self._star_hsplitter = QSplitter(Qt.Orientation.Horizontal)
        self._right_vsplitter.addWidget(self._star_hsplitter)

        # 中央: ScoreSaber ★別（クリア数 + AvgAcc 比較）
        self._ss_cmp_container = QWidget()
        _ss_cmp_vbox = QVBoxLayout(self._ss_cmp_container)
        _ss_cmp_vbox.setContentsMargins(0, 0, 0, 0)
        _ss_cmp_vbox.setSpacing(2)
        self._ss_star_title_label = QLabel("", self._ss_cmp_container)
        self._ss_star_title_label.setStyleSheet("font-size: 11px; padding: 1px 2px;")
        self._ss_star_title_label.setOpenExternalLinks(True)
        _ss_cmp_vbox.addWidget(self._ss_star_title_label)
        self._star_hsplitter.addWidget(self._ss_cmp_container)

        self.ss_star_table = QTableWidget(0, 17, self._ss_cmp_container)
        self.ss_star_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.ss_star_table.setStyleSheet(table_stylesheet())
        self.ss_star_table.verticalHeader().setDefaultSectionSize(18)
        self.ss_star_table.verticalHeader().setMinimumSectionSize(0)
        self.ss_star_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.ss_star_table.setHorizontalHeaderLabels([
            "★",
            "A Clear",
            "B Clear",
            "ΔClear",
            "A FC",
            "B FC",
            "ΔFC",
            "A AvgAcc",
            "B AvgAcc",
            "ΔAcc",
            "★",
            "A PP",
            "B PP",
            "ΔPP",
            "A SPP",
            "B SPP",
            "ΔSPP",
        ])
        ss_star_header = self.ss_star_table.horizontalHeader()
        ss_star_header.setStretchLastSection(False)
        for _c in range(17):
            ss_star_header.setSectionResizeMode(_c, QHeaderView.ResizeMode.Interactive)
        ss_star_header.setSectionResizeMode(3,  QHeaderView.ResizeMode.ResizeToContents)
        ss_star_header.setSectionResizeMode(6,  QHeaderView.ResizeMode.ResizeToContents)
        ss_star_header.setSectionResizeMode(9,  QHeaderView.ResizeMode.ResizeToContents)
        ss_star_header.setSectionResizeMode(13, QHeaderView.ResizeMode.ResizeToContents)
        ss_star_header.setSectionResizeMode(16, QHeaderView.ResizeMode.ResizeToContents)
        ss_star_header.resizeSection(0, 35)
        ss_star_header.resizeSection(1, 90)
        ss_star_header.resizeSection(2, 90)
        ss_star_header.resizeSection(4, 90)
        ss_star_header.resizeSection(5, 90)
        ss_star_header.resizeSection(7, 90)
        ss_star_header.resizeSection(8, 90)
        ss_star_header.resizeSection(10, 35)
        ss_star_header.resizeSection(11, 45)
        ss_star_header.resizeSection(12, 45)
        ss_star_header.resizeSection(14, 50)
        ss_star_header.resizeSection(15, 50)
        # 下段★テーブルは行番号(No)が紛らわしいので非表示にする
        _ss_cmp_vbox.addWidget(self.ss_star_table)
        self.ss_star_table.verticalHeader().setVisible(False)

        # 右: BeatLeader ★別（クリア数 + AvgAcc 比較）
        self._bl_cmp_container = QWidget()
        _bl_cmp_vbox = QVBoxLayout(self._bl_cmp_container)
        _bl_cmp_vbox.setContentsMargins(0, 0, 0, 0)
        _bl_cmp_vbox.setSpacing(2)
        self._bl_star_title_label = QLabel("", self._bl_cmp_container)
        self._bl_star_title_label.setStyleSheet("font-size: 11px; padding: 1px 2px;")
        self._bl_star_title_label.setOpenExternalLinks(True)
        _bl_cmp_vbox.addWidget(self._bl_star_title_label)
        self._star_hsplitter.addWidget(self._bl_cmp_container)

        self.bl_star_table = QTableWidget(0, 17, self._bl_cmp_container)
        self.bl_star_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.bl_star_table.setStyleSheet(table_stylesheet())
        self.bl_star_table.verticalHeader().setDefaultSectionSize(18)
        self.bl_star_table.verticalHeader().setMinimumSectionSize(0)
        self.bl_star_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.bl_star_table.setHorizontalHeaderLabels([
            "★",
            "A Clear",
            "B Clear",
            "ΔClear",
            "A FC",
            "B FC",
            "ΔFC",
            "A AvgAcc",
            "B AvgAcc",
            "ΔAcc(L / R)",
            "★",
            "A PP",
            "B PP",
            "ΔPP",
            "A SPP",
            "B SPP",
            "ΔSPP",
        ])
        bl_star_header = self.bl_star_table.horizontalHeader()
        bl_star_header.setStretchLastSection(False)
        for _c in range(17):
            bl_star_header.setSectionResizeMode(_c, QHeaderView.ResizeMode.Interactive)
        bl_star_header.setSectionResizeMode(3,  QHeaderView.ResizeMode.ResizeToContents)
        bl_star_header.setSectionResizeMode(6,  QHeaderView.ResizeMode.ResizeToContents)
        bl_star_header.setSectionResizeMode(9,  QHeaderView.ResizeMode.ResizeToContents)
        bl_star_header.setSectionResizeMode(13, QHeaderView.ResizeMode.ResizeToContents)
        bl_star_header.setSectionResizeMode(16, QHeaderView.ResizeMode.ResizeToContents)
        bl_star_header.resizeSection(0, 35)
        bl_star_header.resizeSection(1, 90)
        bl_star_header.resizeSection(2, 90)
        bl_star_header.resizeSection(4, 90)
        bl_star_header.resizeSection(5, 90)
        bl_star_header.resizeSection(7, 90)
        bl_star_header.resizeSection(8, 90)
        bl_star_header.resizeSection(10, 35)
        bl_star_header.resizeSection(11, 45)
        bl_star_header.resizeSection(12, 45)
        bl_star_header.resizeSection(14, 50)
        bl_star_header.resizeSection(15, 50)
        _bl_cmp_vbox.addWidget(self.bl_star_table)
        self.bl_star_table.verticalHeader().setVisible(False)

        # 右下: AccSaber 比較グリッド（A/B を列で並べる 1 テーブル形式）
        self._acc_cmp_container = QWidget()
        _acc_cmp_vbox = QVBoxLayout(self._acc_cmp_container)
        _acc_cmp_vbox.setContentsMargins(2, 2, 2, 2)
        _acc_cmp_vbox.setSpacing(2)
        self._right_vsplitter.addWidget(self._acc_cmp_container)

        # タイトルラベル（プレイヤー名と日時）
        self._acc_cmp_title_label = QLabel("", self._acc_cmp_container)
        self._acc_cmp_title_label.setStyleSheet("font-size: 11px; padding: 1px 2px;")
        self._acc_cmp_title_label.setOpenExternalLinks(True)
        _acc_cmp_vbox.addWidget(self._acc_cmp_title_label)

        # 1 テーブル: 4行 × 13列
        # 列: (icon) | A AP | B AP | ΔAP | A Rank | B Rank | ΔRank | A Play Count | B Play Count | ΔPlay Count | A Avg Acc | B Avg Acc | ΔAvg Acc
        self.acc_cmp_table = QTableWidget(4, 13, self._acc_cmp_container)
        self.acc_cmp_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.acc_cmp_table.setStyleSheet(table_stylesheet())
        self.acc_cmp_table.verticalHeader().setDefaultSectionSize(18)
        self.acc_cmp_table.verticalHeader().setVisible(False)
        self.acc_cmp_table.setHorizontalHeaderLabels([
            "", "A AP", "B AP", "\u0394AP",
            "A Rank", "B Rank", "\u0394Rank",
            "A Plays", "B Plays", "\u0394Plays",
            "A Avg Acc", "B Avg Acc", "\u0394Avg Acc",
        ])
        _acc_h = self.acc_cmp_table.horizontalHeader()
        _acc_h.setStretchLastSection(False)
        _acc_h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        # Δ列は ResizeToContents、A/B値列は Interactive で余裕幅を確保
        for _c in (3, 6, 9, 12):
            _acc_h.setSectionResizeMode(_c, QHeaderView.ResizeMode.ResizeToContents)
        for _c in (1, 2, 4, 5, 7, 8, 10, 11):
            _acc_h.setSectionResizeMode(_c, QHeaderView.ResizeMode.Interactive)
        _acc_h.resizeSection(0, 68)
        # A/B 値列の初期幅（コンテンツ幅より少し広め）
        for _c in (1, 2):   # A AP / B AP
            _acc_h.resizeSection(_c, 70)
        for _c in (4, 5):   # A Rank / B Rank
            _acc_h.resizeSection(_c, 80)
        for _c in (7, 8):   # A Plays / B Plays
            _acc_h.resizeSection(_c, 85)
        for _c in (10, 11):  # A Avg Acc / B Avg Acc
            _acc_h.resizeSection(_c, 85)
        _acc_cmp_vbox.addWidget(self.acc_cmp_table)

        # パーセンテージ列に横棒グラフを表示するデリゲートを適用
        # NOTE:
        # delegate の所有関係を明確にするため parent は dialog(self) に統一し、
        # 参照を self 側で保持する。
        self._table_delegates: list[QStyledItemDelegate] = []

        def _bind_delegate(table: QTableWidget, column: int, delegate: QStyledItemDelegate) -> None:
            self._table_delegates.append(delegate)
            table.setItemDelegateForColumn(column, delegate)

        # 新列順: ★|Clear群|FC群|AvgAcc群|★|PP群|SPP群
        # Clear 列 (A Clear / B Clear) のカッコ内の % と、AvgAcc 列にバーを表示する。
        # ScoreSaber 側
        _bind_delegate(self.ss_star_table, 1, PercentageBarDelegate(self, max_value=100.0, gradient_min=0.0))   # A Clear
        _bind_delegate(self.ss_star_table, 2, PercentageBarDelegate(self, max_value=100.0, gradient_min=0.0))   # B Clear
        _bind_delegate(self.ss_star_table, 4, PercentageBarDelegate(self, max_value=100.0, gradient_min=0.0))   # A FC
        _bind_delegate(self.ss_star_table, 5, PercentageBarDelegate(self, max_value=100.0, gradient_min=0.0))   # B FC
        _bind_delegate(self.ss_star_table, 7, PercentageBarDelegate(self, max_value=100.0, gradient_min=75.0))  # A AvgAcc
        _bind_delegate(self.ss_star_table, 8, PercentageBarDelegate(self, max_value=100.0, gradient_min=75.0))  # B AvgAcc
        _bind_delegate(self.ss_star_table, 11, ColumnMaxBarDelegate(self))                                       # A PP
        _bind_delegate(self.ss_star_table, 12, ColumnMaxBarDelegate(self))                                       # B PP

        # BeatLeader 側
        _bind_delegate(self.bl_star_table, 1, PercentageBarDelegate(self, max_value=100.0, gradient_min=0.0))   # A Clear
        _bind_delegate(self.bl_star_table, 2, PercentageBarDelegate(self, max_value=100.0, gradient_min=0.0))   # B Clear
        _bind_delegate(self.bl_star_table, 4, PercentageBarDelegate(self, max_value=100.0, gradient_min=0.0))   # A FC
        _bind_delegate(self.bl_star_table, 5, PercentageBarDelegate(self, max_value=100.0, gradient_min=0.0))   # B FC
        _bind_delegate(self.bl_star_table, 7, PercentageBarDelegate(self, max_value=100.0, gradient_min=75.0))  # A AvgAcc
        _bind_delegate(self.bl_star_table, 8, PercentageBarDelegate(self, max_value=100.0, gradient_min=75.0))  # B AvgAcc
        _bind_delegate(self.bl_star_table, 11, ColumnMaxBarDelegate(self))                                       # A PP
        _bind_delegate(self.bl_star_table, 12, ColumnMaxBarDelegate(self))                                       # B PP

        # AccSaber 比較グリッド: A/B Play Count (col 7,8) と A/B Avg Acc (col 10,11) にデリゲートを設定
        _bind_delegate(self.acc_cmp_table, 7, AccPlayCountBarDelegate(self))   # A Play Count
        _bind_delegate(self.acc_cmp_table, 8, AccPlayCountBarDelegate(self))   # B Play Count
        _bind_delegate(self.acc_cmp_table, 10, PercentageBarDelegate(self, max_value=100.0, gradient_min=70.0))  # A Avg Acc
        _bind_delegate(self.acc_cmp_table, 11, PercentageBarDelegate(self, max_value=100.0, gradient_min=70.0))  # B Avg Acc

        # Metric テーブル: A/B 列 (col 2/3) に AccSaber Play Count / Avg Acc のバーを描画
        _bind_delegate(self.table, 2, AccPlayCountBarDelegate(self))
        _bind_delegate(self.table, 3, AccPlayCountBarDelegate(self))

        # AccSaber テーブル (table_acc): 同様にバーを描画
        _bind_delegate(self.table_acc, 2, AccPlayCountBarDelegate(self))
        _bind_delegate(self.table_acc, 3, AccPlayCountBarDelegate(self))

        self._v_splitter.addWidget(self._splitter)
        self._v_splitter.setCollapsible(0, True)
        self._v_splitter.setCollapsible(1, False)
        # デフォルトの分割比率
        self._star_hsplitter.setOrientation(Qt.Orientation.Vertical)
        self._splitter.setSizes([440, 1045])
        self._v_splitter.setSizes([53, 660])
        self._right_vsplitter.setSizes([615, 190])
        self._star_hsplitter.setSizes([392, 485])
        self._metric_vsplitter.setSizes([367, 436])

        self._load_snapshots()
        # Stats 画面から steam_id が渡されている場合はそちらを優先し、
        # そのプレイヤーについて「最後に選択していたスナップショット日付」を復元する。
        # steam_id が渡されていない場合のみ、従来通りダイアログ全体の前回状態を復元する。
        if self._initial_steam_id is None:
            self._restore_last_selection()
        else:
            self._restore_last_selection_for_player(self._initial_steam_id)

        # 設定ファイルが存在しない場合のデフォルト可視状態を保証する
        # （_restore_ui_state が呼ばれた場合は QTimer で上書きされる）
        self._acc_cmp_container.setVisible(self._acc_position == "Bottom")
        self._acc_metric_container.setVisible(self._acc_position == "Left")
        self.btn_acc_pos.setText("Acc⇦" if self._acc_position == "Bottom" else "Acc⇩")

        self.combo_player_a.currentIndexChanged.connect(self._on_player_a_changed)
        self.combo_player_b.currentIndexChanged.connect(self._on_player_b_changed)
        self.combo_a.currentIndexChanged.connect(self._on_snapshot_a_changed)
        self.combo_b.currentIndexChanged.connect(self._on_snapshot_b_changed)
        self.button_latest_b.clicked.connect(self._on_select_latest_b)
        self.btn_toggle_ss.toggled.connect(self._on_toggle_ss)
        self.btn_toggle_bl.toggled.connect(self._on_toggle_bl)
        self.btn_bl_below.clicked.connect(self._on_toggle_bl_below)
        self.btn_toggle_metric.toggled.connect(self._on_toggle_metric)
        self.btn_toggle_header.toggled.connect(self._on_toggle_header)
        self.btn_acc_as.clicked.connect(self._on_acc_mode_as)
        self.btn_acc_rl.clicked.connect(self._on_acc_mode_rl)
        self.btn_acc_pos.clicked.connect(self._on_acc_position_toggle)
        self.chk_col_clear.toggled.connect(self._apply_star_col_visibility)
        self.chk_col_fc.toggled.connect(self._apply_star_col_visibility)
        self.chk_col_acc.toggled.connect(self._apply_star_col_visibility)
        self.chk_col_pp.toggled.connect(self._apply_star_col_visibility)
        self.chk_col_starpp.toggled.connect(self._apply_star_col_visibility)
        self.btn_row_height_up.clicked.connect(self._on_row_height_up)
        self.btn_row_height_dn.clicked.connect(self._on_row_height_dn)
        self.btn_default_layout.clicked.connect(self._on_default_layout)
        self._splitter.splitterMoved.connect(lambda *_: self._save_last_selection())
        self._right_vsplitter.splitterMoved.connect(lambda *_: self._save_last_selection())
        self._star_hsplitter.splitterMoved.connect(lambda *_: self._save_last_selection())
        self._metric_vsplitter.splitterMoved.connect(lambda *_: self._save_last_selection())

        self._apply_row_height()
        self._update_view2()
        if not self._ui_state_restored:
            QTimer.singleShot(0, self._apply_default_layout_initial_geometry)

    # -------------------- internal helpers --------------------

    def _load_snapshots(self) -> None:
        """snapshots ディレクトリから JSON を読み込んでコンボに並べる。"""
        self.combo_player_a.clear()
        self.combo_player_b.clear()
        self.combo_a.clear()
        self.combo_b.clear()
        self._snapshots_by_player.clear()
        
        # print文は日本語で
        print("スナップショットを読み込んでいます:", SNAPSHOT_DIR)
        # 日付の新しい順（降順）で読み込みつつ、プレイヤーごとにグループ化
        paths: List[Path] = sorted(SNAPSHOT_DIR.glob("*.json"), reverse=True)
        for path in paths:
            try:
                snap = Snapshot.load(path)
            except Exception:
                continue

            sid = snap.steam_id
            if not sid:
                continue
            self._snapshots_by_player.setdefault(sid, []).append(snap)

        # 各プレイヤーごとに、時刻の新しい順にソート
        for sid, snaps in self._snapshots_by_player.items():
            snaps.sort(key=lambda s: s.taken_at, reverse=True)

        # プレイヤー選択コンボを構築（最新スナップショットの名前を使う）
        for sid, snaps in sorted(self._snapshots_by_player.items()):
            latest = snaps[0]
            print("最新のスナップショット (プレイヤー):", sid, latest.taken_at)
            name = latest.scoresaber_name or latest.beatleader_name or ""
            print("プレイヤーをコンボに追加:", sid, name)
            if name:
                label = f"{name} ({sid})"
            else:
                label = sid
            self.combo_player_a.addItem(label, userData=sid)
            self.combo_player_b.addItem(label, userData=sid)

        # 既定では Stats 画面から渡されたプレイヤーIDを優先して選択し、無ければ先頭
        if self.combo_player_a.count() > 0:
            default_index = 0
            if self._initial_steam_id:
                for i in range(self.combo_player_a.count()):
                    data = self.combo_player_a.itemData(i)
                    if isinstance(data, str) and data == self._initial_steam_id:
                        default_index = i
                        break

            self.combo_player_a.setCurrentIndex(default_index)
            self.combo_player_b.setCurrentIndex(default_index)
            self._reload_player_snapshots_for(self.combo_player_a, self.combo_a)
            self._reload_player_snapshots_for(self.combo_player_b, self.combo_b)

    def _apply_plain_header_button_style(self) -> None:
        """比較画面ヘッダーのプレーンボタン余白をテーマ間で揃える。"""
        button_qss = ""
        if not is_dark():
            button_qss = (
                "QPushButton {"
                "margin: 0px; padding: 0px 10px;"
                "background-color: #f6f6f6; color: #111111;"
                "border: 1px solid #d9d9d9; border-radius: 6px;"
                "}"
                "QPushButton:hover { background-color: #ececec; border-color: #c8c8c8; }"
                "QPushButton:pressed { background-color: #e2e2e2; }"
            )
        for button in getattr(self, "_plain_header_buttons", []):
            button.setStyleSheet(button_qss)

    def _apply_header_button_density(self) -> None:
        min_height = _light_app_button_min_height()
        for button in getattr(self, "_top_control_buttons", []):
            button.setMinimumHeight(min_height)

    # 設定保存/復元まわり

    def _settings_path(self) -> Path:
        """比較ダイアログ用の設定ファイルパスを返す。"""

        cache_dir = BASE_DIR / "cache"
        return cache_dir / "snapshot_compare.json"

    def _restore_last_selection_for_player(self, steam_id: str) -> None:
        """指定プレイヤーについて、最後に選択していたスナップショット日付を復元する。"""

        path = self._settings_path()
        if not path.exists():
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return

        per_player = data.get("per_player")
        if not isinstance(per_player, dict):
            return

        entry = per_player.get(steam_id)
        if not isinstance(entry, dict):
            return

        snap_a_taken_at = entry.get("snapshot_a_taken_at") or None
        snap_b_taken_at = entry.get("snapshot_b_taken_at") or None

        snaps = self._snapshots_by_player.get(steam_id) or []
        if not snaps:
            return

        def _apply_snapshot_selection(snap_combo: QComboBox, taken_at: Optional[str]) -> None:
            if not taken_at:
                return

            target_index = -1
            for idx, snap in enumerate(snaps):
                if snap.taken_at == taken_at:
                    target_index = idx
                    break

            if 0 <= target_index < snap_combo.count():
                snap_combo.setCurrentIndex(target_index)

        _apply_snapshot_selection(self.combo_a, snap_a_taken_at)
        _apply_snapshot_selection(self.combo_b, snap_b_taken_at)
        self._restore_ui_state(data)

    def _restore_last_selection(self) -> None:
        """前回のプレイヤー/スナップショット選択状態を可能な範囲で復元する。"""

        path = self._settings_path()
        if not path.exists():
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return

        # 旧フォーマット（player_a / snapshot_a_taken_at ...）を優先して扱う。
        # 新フォーマット(per_player)のみ存在する場合は、ここでは特に何もしない
        # （Stats 画面からの起動時に _restore_last_selection_for_player を使うため）。
        player_a_id = data.get("player_a") or None
        player_b_id = data.get("player_b") or None
        snap_a_taken_at = data.get("snapshot_a_taken_at") or None
        snap_b_taken_at = data.get("snapshot_b_taken_at") or None

        def _select_player_and_snapshot(
            player_combo: QComboBox,
            snap_combo: QComboBox,
            player_id: Optional[str],
            taken_at: Optional[str],
        ) -> None:
            if not player_id:
                return

            # プレイヤーコンボから該当 SteamID を探す
            target_index = -1
            for i in range(player_combo.count()):
                value = player_combo.itemData(i)
                if isinstance(value, str) and value == player_id:
                    target_index = i
                    break

            if target_index < 0:
                return

            player_combo.setCurrentIndex(target_index)
            self._reload_player_snapshots_for(player_combo, snap_combo)

            if not taken_at:
                return

            snaps = self._snapshots_by_player.get(player_id) or []
            target_snap_index = -1
            for idx, snap in enumerate(snaps):
                if snap.taken_at == taken_at:
                    target_snap_index = idx
                    break

            if 0 <= target_snap_index < snap_combo.count():
                snap_combo.setCurrentIndex(target_snap_index)

        _select_player_and_snapshot(self.combo_player_a, self.combo_a, player_a_id, snap_a_taken_at)
        _select_player_and_snapshot(self.combo_player_b, self.combo_b, player_b_id, snap_b_taken_at)
        self._restore_ui_state(data)

    def _restore_ui_state(self, data: dict) -> None:
        """JSON data からトグル/チェックボックスの状態を復元する。"""
        self._ui_state_restored = True
        acc_mode = data.get("ui_acc_mode")
        if acc_mode in ("AS", "RL"):
            self._acc_mode = acc_mode
            self.btn_acc_as.setChecked(acc_mode == "AS")
            self.btn_acc_rl.setChecked(acc_mode == "RL")
        acc_position = data.get("ui_acc_position")
        if acc_position in ("Left", "Bottom"):
            self._acc_position = acc_position
            self.btn_acc_pos.setText("Acc⇦" if acc_position == "Bottom" else "Acc⇩")
        if "ui_toggle_bl_below" in data:
            self._bl_below = bool(data["ui_toggle_bl_below"])
        for btn, key in (
            (self.btn_toggle_metric, "ui_toggle_metric"),
            (self.btn_toggle_ss,     "ui_toggle_ss"),
            (self.btn_toggle_bl,     "ui_toggle_bl"),
            (self.btn_toggle_header, "ui_toggle_header"),
        ):
            if key in data:
                btn.blockSignals(True)
                btn.setChecked(bool(data[key]))
                btn.blockSignals(False)
        for chk, key in (
            (self.chk_col_clear,  "ui_col_clear"),
            (self.chk_col_fc,     "ui_col_fc"),
            (self.chk_col_acc,    "ui_col_acc"),
            (self.chk_col_pp,     "ui_col_pp"),
            (self.chk_col_starpp, "ui_col_starpp"),
        ):
            if key in data:
                chk.blockSignals(True)
                chk.setChecked(bool(data[key]))
                chk.blockSignals(False)
        # 可視性を一択適用
        self._metric_cmp_container.setVisible(self.btn_toggle_metric.isChecked())
        self._ss_cmp_container.setVisible(self.btn_toggle_ss.isChecked())
        self._bl_cmp_container.setVisible(self.btn_toggle_bl.isChecked())
        self.btn_bl_below.setText("BL⇨" if self._bl_below else "BL⇩")
        self._star_hsplitter.setOrientation(
            Qt.Orientation.Vertical if self._bl_below else Qt.Orientation.Horizontal
        )
        # タイトルヘッダの可視性を適用
        _hdr = self.btn_toggle_header.isChecked()
        self._metric_title_label.setVisible(_hdr)
        self._ss_star_title_label.setVisible(_hdr)
        self._bl_star_title_label.setVisible(_hdr)
        self._acc_cmp_title_label.setVisible(_hdr)
        self._acc_metric_title_label.setVisible(_hdr)
        # 縦スプリッターの上パネルサイズを復元
        if "ui_v_splitter_top" in data:
            _top_h = int(data["ui_v_splitter_top"])
            QTimer.singleShot(0, lambda h=_top_h: self._restore_v_splitter(h))
        # 行高を復元して全テーブルに適用
        if "ui_row_height" in data:
            self._row_height = max(8, min(40, int(data["ui_row_height"])))
        self._apply_row_height()
        # ウィンドウサイズを復元（スプリッターサイズに先立って適用する）
        if "ui_window_width" in data and "ui_window_height" in data:
            self.resize(int(data["ui_window_width"]), int(data["ui_window_height"]))
        # スプリッター位置を復元（描画前なので saved_ 変数に入れておき、_rebalance_splitter で使う）
        if "ui_splitter_sizes" in data:
            v = data["ui_splitter_sizes"]
            if isinstance(v, list) and len(v) == 2:
                self._saved_splitter_sizes = [int(x) for x in v]
        if "ui_right_vsplitter_sizes" in data:
            v = data["ui_right_vsplitter_sizes"]
            if isinstance(v, list) and len(v) == 2:
                self._saved_right_vsplitter_sizes = [int(x) for x in v]
        if "ui_star_hsplitter_ss" in data:
            self._saved_star_hsplitter_ss = int(data["ui_star_hsplitter_ss"])
        if "ui_metric_vsplitter_sizes" in data:
            v = data["ui_metric_vsplitter_sizes"]
            if isinstance(v, list) and len(v) == 2:
                self._saved_metric_vsplitter_sizes = [int(x) for x in v]
        # 描画完了後にスプリッター位置を適用する
        def _restore_splitters():
            self._right_vsplitter.setSizes(self._saved_right_vsplitter_sizes)
            self._metric_vsplitter.setSizes(self._saved_metric_vsplitter_sizes)
            self._rebalance_splitter()
            self._apply_acc_position()
        QTimer.singleShot(0, _restore_splitters)
        self._apply_star_col_visibility_inner()

    def _save_last_selection(self) -> None:
        """現在のプレイヤー/スナップショット選択状態と UI 状態を設定ファイルに保存する。"""

        snap_a = self._current_snapshot(self.combo_a)
        snap_b = self._current_snapshot(self.combo_b)

        path = self._settings_path()
        try:
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    data = {}
            else:
                data = {}

            # UI 状態（トグルボタン・チェックボックス）を常に保存
            data["ui_acc_mode"]       = self._acc_mode
            data["ui_acc_position"]   = self._acc_position
            data["ui_toggle_metric"]  = self.btn_toggle_metric.isChecked()
            data["ui_toggle_ss"]      = self.btn_toggle_ss.isChecked()
            data["ui_toggle_bl"]      = self.btn_toggle_bl.isChecked()
            data["ui_toggle_bl_below"] = self._bl_below
            data["ui_toggle_header"]       = self.btn_toggle_header.isChecked()
            data["ui_row_height"]          = self._row_height
            data["ui_v_splitter_top"]      = self._v_splitter.sizes()[0]
            # メインスプリッター・右縦スプリッター・SS/BL横スプリッターの位置を保存
            _sp = self._splitter.sizes()
            if sum(_sp) > 0 and self._metric_cmp_container.isVisible():
                self._saved_splitter_sizes = list(_sp)
            data["ui_splitter_sizes"]          = self._saved_splitter_sizes
            _rvsp = self._right_vsplitter.sizes()
            if sum(_rvsp) > 0 and self._acc_cmp_container.isVisible():
                self._saved_right_vsplitter_sizes = list(_rvsp)
            data["ui_right_vsplitter_sizes"]   = self._saved_right_vsplitter_sizes
            _shs = self._star_hsplitter.sizes()
            if _shs[0] > 0 and self._ss_cmp_container.isVisible():
                self._saved_star_hsplitter_ss = _shs[0]
            data["ui_star_hsplitter_ss"]       = self._saved_star_hsplitter_ss
            _mvsp = self._metric_vsplitter.sizes()
            if sum(_mvsp) > 0 and self._acc_metric_container.isVisible():
                self._saved_metric_vsplitter_sizes = list(_mvsp)
            data["ui_metric_vsplitter_sizes"] = self._saved_metric_vsplitter_sizes
            # ウィンドウサイズを保存
            data["ui_window_width"]  = self.width()
            data["ui_window_height"] = self.height()
            data["ui_col_clear"]      = self.chk_col_clear.isChecked()
            data["ui_col_fc"]         = self.chk_col_fc.isChecked()
            data["ui_col_acc"]        = self.chk_col_acc.isChecked()
            data["ui_col_pp"]        = self.chk_col_pp.isChecked()
            data["ui_col_starpp"]    = self.chk_col_starpp.isChecked()

            # スナップショット未選択の場合は UI 状態のみ保存して終了
            if snap_a is None and snap_b is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                return

            # 互換性のため、従来のトップレベル情報も更新しておく
            if snap_a is not None:
                data["player_a"] = snap_a.steam_id
                data["snapshot_a_taken_at"] = snap_a.taken_at
            if snap_b is not None:
                data["player_b"] = snap_b.steam_id
                data["snapshot_b_taken_at"] = snap_b.taken_at

            # 新フォーマット: プレイヤーごとに最後に選択したスナップショットを保持する
            per_player = data.get("per_player")
            if not isinstance(per_player, dict):
                per_player = {}
                data["per_player"] = per_player

            if snap_a is not None and snap_a.steam_id:
                entry = per_player.get(snap_a.steam_id)
                if not isinstance(entry, dict):
                    entry = {}
                entry["snapshot_a_taken_at"] = snap_a.taken_at
                per_player[snap_a.steam_id] = entry

            if snap_b is not None and snap_b.steam_id:
                entry = per_player.get(snap_b.steam_id)
                if not isinstance(entry, dict):
                    entry = {}
                entry["snapshot_b_taken_at"] = snap_b.taken_at
                per_player[snap_b.steam_id] = entry

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            # 設定保存失敗はアプリ動作に影響させない
            return

    def _reload_player_snapshots_for(self, player_combo: QComboBox, snap_combo: QComboBox) -> None:
        """指定プレイヤーのスナップショット一覧を指定プルダウンに反映する。"""

        snap_combo.clear()

        data = player_combo.currentData()
        sid = data if isinstance(data, str) else None
        if not sid:
            return

        snaps = self._snapshots_by_player.get(sid) or []
        if not snaps:
            return

        # taken_at は UTC(Z) で保存しているので、ローカル時刻に変換して表示する
        def _format_label(snap: Snapshot) -> str:
            """スナップショットの表示ラベル（日時のみ）を生成する。"""

            taken_text = snap.taken_at
            try:
                t_str = snap.taken_at
                if t_str.endswith("Z"):
                    t_str = t_str[:-1]
                    dt_utc = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
                else:
                    dt_utc = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
                dt_local = dt_utc.astimezone()
                taken_text = dt_local.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                taken_text = snap.taken_at
            return taken_text

        for snap in snaps:
            label = _format_label(snap)
            snap_combo.addItem(label)

        # デフォルトでは最新のスナップショットを選択
        if snap_combo.count() > 0:
            snap_combo.setCurrentIndex(0)

    def _on_player_a_changed(self, index: int) -> None:  # noqa: ARG002
        self._reload_player_snapshots_for(self.combo_player_a, self.combo_a)
        self._update_view2()
        self._save_last_selection()

    def _on_player_b_changed(self, index: int) -> None:  # noqa: ARG002
        self._reload_player_snapshots_for(self.combo_player_b, self.combo_b)
        self._update_view2()
        self._save_last_selection()

    def _on_snapshot_a_changed(self, index: int) -> None:  # noqa: ARG002
        self._update_view2()
        self._save_last_selection()

    def _on_snapshot_b_changed(self, index: int) -> None:  # noqa: ARG002
        self._update_view2()
        self._save_last_selection()

    def _on_select_latest_b(self) -> None:
        """Snapshot B を現在のプレイヤーの最新スナップショットに戻す。"""

        if self.combo_b.count() == 0:
            return
        # インデックス 0 には常に最新スナップショットを並べている想定
        if self.combo_b.currentIndex() != 0:
            self.combo_b.setCurrentIndex(0)
        else:
            # 既に最新が選択済みの場合は明示的に更新だけ行う
            self._update_view2()
            self._save_last_selection()

    def _apply_row_height(self) -> None:
        """全テーブルの行高を self._row_height に統一して適用する。"""
        h = self._row_height
        for tbl in (self.table, self.table_acc, self.ss_star_table, self.bl_star_table, self.acc_cmp_table):
            tbl.verticalHeader().setMinimumSectionSize(0)
            tbl.verticalHeader().setDefaultSectionSize(h)
            tbl.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)

    def _on_row_height_up(self) -> None:
        """行の高さを 1 大きくする。"""
        self._row_height = min(self._row_height + 1, 40)
        self._apply_row_height()
        self._save_last_selection()

    def _on_row_height_dn(self) -> None:
        """行の高さを 1 小さくする。"""
        self._row_height = max(self._row_height - 1, 8)
        self._apply_row_height()
        self._save_last_selection()

    def _on_default_layout(self) -> None:
        """ボタン状態をリセットしてからレイアウトをデフォルト値に戻す。"""
        # --- ボタン状態リセット（シグナルを一時ブロックして副作用を防ぐ） ---
        for btn in (self.btn_toggle_metric, self.btn_toggle_ss, self.btn_toggle_bl):
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)

        # --- チェックボックス全オン ---
        for chk in (self.chk_col_clear, self.chk_col_fc, self.chk_col_acc, self.chk_col_pp, self.chk_col_starpp):
            chk.blockSignals(True)
            chk.setChecked(True)
            chk.blockSignals(False)
        self._apply_star_col_visibility()

        self._bl_below = True
        self.btn_bl_below.setText("BL⇨")
        self._star_hsplitter.setOrientation(Qt.Orientation.Vertical)

        self._acc_mode = "RL"
        self.btn_acc_as.setChecked(False)
        self.btn_acc_rl.setChecked(True)

        self._acc_position = "Left"
        self.btn_acc_pos.setText("Acc⇩")

        # コンテナ表示状態を確定させる
        self._metric_cmp_container.setVisible(True)
        self._ss_cmp_container.setVisible(True)
        self._bl_cmp_container.setVisible(True)

        # --- サイズ・行高リセット ---
        self._row_height = 21
        self._saved_splitter_sizes = [440, 1045]
        self._saved_right_vsplitter_sizes = [615, 190]
        self._saved_star_hsplitter_ss = 392
        self._saved_metric_vsplitter_sizes = [367, 436]
        self.resize(1540, 880)
        self._apply_row_height()
        self._update_view2()

        self._apply_default_layout_initial_geometry(save=False)

        def _save() -> None:
            self._save_last_selection()

        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, _save)

    def _apply_default_layout_initial_geometry(self, save: bool = False) -> None:
        """Default Layout 相当の描画後ジオメトリを適用する。"""

        self._right_vsplitter.setSizes(self._saved_right_vsplitter_sizes)
        self._metric_vsplitter.setSizes(self._saved_metric_vsplitter_sizes)
        self._rebalance_splitter()
        self._apply_acc_position()
        self._restore_v_splitter(53)
        if save:
            self._save_last_selection()

    def _on_toggle_bl_below(self) -> None:
        """BeatLeader を ScoreSaber の下に配置する / 左右並びに戻す。"""
        self._bl_below = not self._bl_below
        self.btn_bl_below.setText("BL⇨" if self._bl_below else "BL⇩")
        self._star_hsplitter.setOrientation(
            Qt.Orientation.Vertical if self._bl_below else Qt.Orientation.Horizontal
        )
        self._save_last_selection()

    def _on_toggle_metric(self, checked: bool) -> None:
        """Metric テーブルの表示/非表示を切り替える。"""
        self._metric_cmp_container.setVisible(checked)
        self._rebalance_splitter()
        self._save_last_selection()

    def _on_acc_mode_as(self) -> None:
        """AccSaber モードに切り替える。"""
        self._acc_mode = "AS"
        self.btn_acc_as.setChecked(True)
        self.btn_acc_rl.setChecked(False)
        self._update_view2()
        self._save_last_selection()

    def _on_acc_mode_rl(self) -> None:
        """AccSaber Reloaded モードに切り替える。"""
        self._acc_mode = "RL"
        self.btn_acc_as.setChecked(False)
        self.btn_acc_rl.setChecked(True)
        self._update_view2()
        self._save_last_selection()

    def _on_acc_position_toggle(self) -> None:
        """AccSaberの表示位置を左パネル⇔下パネルでトグルする。"""
        if self._acc_position == "Left":
            self._acc_position = "Bottom"
        else:
            self._acc_position = "Left"
        self.btn_acc_pos.setText("Acc⇦" if self._acc_position == "Bottom" else "Acc⇩")
        self._apply_acc_position()
        self._save_last_selection()

    def _on_toggle_ss(self, checked: bool) -> None:
        """ScoreSaber ★別テーブルの表示/非表示を切り替える。"""
        self._ss_cmp_container.setVisible(checked)
        self._rebalance_splitter()
        self._save_last_selection()

    def _on_toggle_bl(self, checked: bool) -> None:
        """BeatLeader ★別テーブルの表示/非表示を切り替える。"""
        self._bl_cmp_container.setVisible(checked)
        self._rebalance_splitter()
        self._save_last_selection()

    def _on_toggle_header(self, checked: bool) -> None:
        """全テーブルのタイトルヘッダの表示/非表示を切り替える。"""
        self._metric_title_label.setVisible(checked)
        self._ss_star_title_label.setVisible(checked)
        self._bl_star_title_label.setVisible(checked)
        self._acc_cmp_title_label.setVisible(checked)
        self._acc_metric_title_label.setVisible(checked)
        self._save_last_selection()

    def _restore_v_splitter(self, top_h: int) -> None:
        """縦スプリッターの上パネルサイズを復元する（QTimer 経由で呼ぶ前提）。"""
        total = sum(self._v_splitter.sizes())
        if total <= 0:
            return
        self._v_splitter.setSizes([top_h, max(0, total - top_h)])

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """ダイアログを閉じる前に現在の状態を保存する。"""
        self._save_last_selection()
        super().closeEvent(event)

    def _apply_star_col_visibility(self, *_) -> None:
        """Clear/FC/Acc/PP/SPP 列グループの表示/非表示を SS/BL 両テーブルに適用する。"""
        self._save_last_selection()
        self._apply_star_col_visibility_inner()

    def _on_chk_all(self) -> None:
        """Clear/FC/Acc/PP/SPP すべてをチェックする。"""
        for chk in (self.chk_col_clear, self.chk_col_fc, self.chk_col_acc, self.chk_col_pp, self.chk_col_starpp):
            chk.setChecked(True)

    def _on_chk_none(self) -> None:
        """Clear/FC/Acc/PP/SPP すべてのチェックを外す。"""
        for chk in (self.chk_col_clear, self.chk_col_fc, self.chk_col_acc, self.chk_col_pp, self.chk_col_starpp):
            chk.setChecked(False)

    def _apply_star_col_visibility_inner(self) -> None:
        """Clear/FC/Acc/PP/SPP 列グループの表示/非表示を実際に適用する。"""
        clear_vis   = self.chk_col_clear.isChecked()
        fc_vis      = self.chk_col_fc.isChecked()
        acc_vis     = self.chk_col_acc.isChecked()
        pp_vis      = self.chk_col_pp.isChecked()
        starpp_vis  = self.chk_col_starpp.isChecked()

        for table in (self.ss_star_table, self.bl_star_table):
            # col 0 は Clear/FC/Acc グループ用 ★ — それらが全て非表示なら隠す
            table.setColumnHidden(0, not (clear_vis or fc_vis or acc_vis))
            for col in (1, 2, 3):   # A Clear / B Clear / ΔClear
                table.setColumnHidden(col, not clear_vis)
            for col in (4, 5, 6):   # A FC / B FC / ΔFC
                table.setColumnHidden(col, not fc_vis)
            for col in (7, 8, 9):   # A AvgAcc / B AvgAcc / ΔAcc
                table.setColumnHidden(col, not acc_vis)
            # col 10 は ★ 区切り列 — PP または SPP を表示するときのみ表示
            table.setColumnHidden(10, not (pp_vis or starpp_vis))
            for col in (11, 12, 13):  # A PP / B PP / ΔPP
                table.setColumnHidden(col, not pp_vis)
            for col in (14, 15, 16):  # A SPP / B SPP / ΔSPP
                table.setColumnHidden(col, not starpp_vis)

    def _rebalance_splitter(self) -> None:
        """SS/BL/Metric テーブルの表示状態に応じてスプリッタサイズを再調整する。"""
        sizes = self._splitter.sizes()
        total = sum(sizes)
        metric_vis = self._metric_cmp_container.isVisible()
        ss_vis = self._ss_cmp_container.isVisible()
        bl_vis = self._bl_cmp_container.isVisible()

        # Metric は保存幅を使う
        metric_w = self._saved_splitter_sizes[0] if metric_vis else 0

        remaining = max(0, total - metric_w)
        self._splitter.setSizes([metric_w, remaining])

        # _star_hsplitter 内の SS/BL テーブルのサイズを調整
        star_sizes = self._star_hsplitter.sizes()
        star_total = sum(star_sizes)
        if star_total <= 0:
            star_total = sum(self._saved_star_hsplitter_ss + 485 for _ in range(1))
        if ss_vis and bl_vis:
            ss_w = self._saved_star_hsplitter_ss
            self._star_hsplitter.setSizes([max(0, ss_w), max(0, star_total - ss_w)])
        elif ss_vis:
            self._star_hsplitter.setSizes([star_total, 0])
        elif bl_vis:
            self._star_hsplitter.setSizes([0, star_total])
        else:
            self._star_hsplitter.setSizes([0, 0])
        self._ss_cmp_container.setVisible(ss_vis)
        self._bl_cmp_container.setVisible(bl_vis)

    def _apply_acc_position(self) -> None:
        """_acc_position に応じて AccSaber 関連コンテナの表示/非表示を切り替え、スプリッターを再調整する。

        - Left モード: 左側リスト (_acc_metric_container) を表示、右下グリッド (_acc_cmp_container) を非表示
        - Bottom モード: 右下グリッド (_acc_cmp_container) を表示、左側リスト (_acc_metric_container) を非表示
        """
        show_metric = (self._acc_position == "Left")
        show_cmp = (self._acc_position == "Bottom")
        self._acc_metric_container.setVisible(show_metric)
        self._acc_cmp_container.setVisible(show_cmp)

        # _metric_vsplitter の再調整
        mv_sizes = self._metric_vsplitter.sizes()
        mv_total = sum(mv_sizes)
        if mv_total > 0:
            if show_metric:
                ss = self._saved_metric_vsplitter_sizes
                s0, s1 = max(1, ss[0]), max(1, ss[1])
                t = s0 + s1
                new_top = int(mv_total * s0 / t)
                self._metric_vsplitter.setSizes([new_top, mv_total - new_top])
            else:
                self._metric_vsplitter.setSizes([mv_total, 0])

        # _right_vsplitter の再調整
        rv_sizes = self._right_vsplitter.sizes()
        rv_total = sum(rv_sizes)
        if rv_total > 0:
            if show_cmp:
                ss = self._saved_right_vsplitter_sizes
                s0, s1 = max(1, ss[0]), max(1, ss[1])
                t = s0 + s1
                new_top = int(rv_total * s0 / t)
                self._right_vsplitter.setSizes([new_top, rv_total - new_top])
            else:
                self._right_vsplitter.setSizes([rv_total, 0])

    def _current_snapshot(self, combo: QComboBox) -> Optional[Snapshot]:
        # A/B それぞれに対応するプレイヤーコンボから現在のプレイヤーを取得
        if combo is self.combo_a:
            player_combo = self.combo_player_a
        elif combo is self.combo_b:
            player_combo = self.combo_player_b
        else:
            return None

        data = player_combo.currentData()
        sid = data if isinstance(data, str) else None
        if not sid:
            return None
        snaps = self._snapshots_by_player.get(sid) or []
        idx = combo.currentIndex()
        if idx < 0 or idx >= len(snaps):
            return None
        return snaps[idx]

    def _set_row(self, table: QTableWidget, row: int, label: str, a, b) -> None:  # type: ignore[name-defined]
        """指定テーブルの1行分の値と差分を設定する。

        a / b には以下のいずれかを渡せる:
        - 数値 (int/float)
        - 文字列
        - (数値, 表示文字列) のタプル

        タプル形式の場合、差分計算には数値を用い、テーブル表示には
        表示文字列を使う。これにより「10 (50.0%)」のような表記でも
        10 同士の差分を計算できる。
        """

        while table.rowCount() <= row:
            table.insertRow(table.rowCount())

        # ラベル先頭の [SS]/[BL]/[AS] をアイコン＋テキストに展開
        original_label = label
        icon = None
        text = label
        if label.startswith("[SS] "):
            icon = self._icon_scoresaber
            text = label[len("[SS] "):]
        elif label.startswith("[BL] "):
            icon = self._icon_beatleader
            text = label[len("[BL] "):]
        elif label.startswith("[AS] "):
            icon = self._icon_accsaber
            text = label[len("[AS] "):]
        elif label.startswith("[RL] "):
            icon = self._icon_accsaber_rl
            text = label[len("[RL] "):]

        item_grp = QTableWidgetItem("")
        item_grp.setBackground(label_cell_color())
        table.setItem(row, 0, item_grp)

        item0 = QTableWidgetItem(text)
        item0.setBackground(label_cell_color())
        item0.setForeground(label_cell_text_color())
        table.setItem(row, 1, item0)

        def _extract(value):
            # (numeric_value, display_text) 形式を解釈する
            if isinstance(value, tuple) and len(value) == 2:
                numeric, display = value
                text = "" if display is None else str(display)
                return numeric, text
            if value is None:
                return None, ""
            if isinstance(value, int):
                return value, f"{value:,}"
            if isinstance(value, float):
                return value, f"{value:,.2f}"
            return value, str(value)

        a_numeric, text_a = _extract(a)
        b_numeric, text_b = _extract(b)

        table.setItem(row, 2, QTableWidgetItem(text_a))
        table.setItem(row, 3, QTableWidgetItem(text_b))

        diff_item = QTableWidgetItem("")
        # diff_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if isinstance(a_numeric, (int, float)) and isinstance(b_numeric, (int, float)):
            # Rank 系の指標は「数値が小さいほど良い」ので符号を反転させる。
            # 例: ランクが 1000 → 900 に改善した場合、+100 として扱う。
            is_rank_metric = ("Rank" in label and "Ranked" not in label)

            diff = b_numeric - a_numeric
            if is_rank_metric:
                diff = -diff
            if isinstance(a_numeric, float) or isinstance(b_numeric, float):
                diff_item.setText(f"{diff:+,.2f}")
            else:
                diff_item.setText(f"{diff:+,d}")

            if diff > 0:
                color = diff_positive_bg()
            elif diff < 0:
                color = diff_negative_bg()
            else:
                color = diff_neutral_bg()
            diff_item.setBackground(color)
            diff_item.setForeground(diff_text_color())

        table.setItem(row, 4, diff_item)

    def _update_view2(self) -> None:
        """スナップショット比較テーブルを更新する（新実装）。"""

        snap_a = self._current_snapshot(self.combo_a)
        snap_b = self._current_snapshot(self.combo_b)
        self.table.setRowCount(0)
        self.table_acc.setRowCount(0)
        self.ss_star_table.setRowCount(0)
        self.bl_star_table.setRowCount(0)
        # setRowCount(0) で垂直ヘッダの設定がリセットされるため再適用する
        self._apply_row_height()
        # AccSaber 比較グリッドのデータをクリア
        for _r in range(4):
            for _c in range(13):
                self.acc_cmp_table.setItem(_r, _c, QTableWidgetItem(""))

        if snap_a is None or snap_b is None:
            return

        # A / B 列ヘッダにスナップショット日付+時刻を含める（例: A (2026/01/11 13:45)）
        def _date_only(taken_at: str) -> str:
            try:
                t_str = taken_at
                if t_str.endswith("Z"):
                    t_str = t_str[:-1]
                    dt_utc = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
                else:
                    dt_utc = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
                dt_local = dt_utc.astimezone()
                return dt_local.strftime("%Y/%m/%d %H:%M")
            except Exception:
                return taken_at

        date_a = _date_only(snap_a.taken_at)
        date_b = _date_only(snap_b.taken_at)

        # プレイヤー名（SteamIDなし）
        def _player_name(snap) -> str:
            return snap.scoresaber_name or snap.beatleader_name or snap.steam_id or ""

        _name_a = _player_name(snap_a)
        _name_b = _player_name(snap_b)
        _same_player = (snap_a.steam_id == snap_b.steam_id)

        # AccSaber 比較グリッドのタイトルラベルを更新
        if _same_player:
            _acc_title = f"{_name_a}&nbsp;&nbsp;&nbsp;<b>A:</b>&nbsp;{date_a}&nbsp;&nbsp;&nbsp;&nbsp;<b>B:</b>&nbsp;{date_b}"
        else:
            _acc_title = f"<b>A:</b>&nbsp;{_name_a}&nbsp;({date_a})&nbsp;&nbsp;&nbsp;&nbsp;<b>B:</b>&nbsp;{_name_b}&nbsp;({date_b})"

        _link_color = "#5aaaee" if is_dark() else "#0066cc"
        _link_style = f"color:{_link_color}; text-decoration:none; font-weight:bold;"
        _ss_id_a = snap_a.scoresaber_id
        _bl_id_a = snap_a.beatleader_id or snap_a.steam_id

        _acc_mode_text = "AccSaber Reloaded" if self._acc_mode == "RL" else "AccSaber"
        if self._acc_mode == "RL":
            _acc_service_url = f"https://accsaberreloaded.com/players/{_bl_id_a}" if _bl_id_a else ""
        else:
            _acc_service_url = f"https://accsaber.com/profile/{_ss_id_a}" if _ss_id_a else ""
        if _acc_service_url:
            _acc_service_text = f'<a href="{_acc_service_url}" style="{_link_style}">{_acc_mode_text}</a>'
        else:
            _acc_service_text = _acc_mode_text
        self._acc_cmp_title_label.setText(f"{_acc_service_text} ／ {_acc_title}")
        self._acc_metric_title_label.setText(f"{_acc_service_text} ／ {_acc_title}")

        # ScoreSaber / BeatLeader ★別テーブルのタイトルラベルを更新
        if _same_player:
            _star_title = f"{_name_a}&nbsp;&nbsp;&nbsp;<b>A:</b>&nbsp;{date_a}&nbsp;&nbsp;&nbsp;&nbsp;<b>B:</b>&nbsp;{date_b}"
        else:
            _star_title = f"<b>A:</b>&nbsp;{_name_a}&nbsp;({date_a})&nbsp;&nbsp;&nbsp;&nbsp;<b>B:</b>&nbsp;{_name_b}&nbsp;({date_b})"
        _ss_url = f"https://scoresaber.com/u/{_ss_id_a}" if _ss_id_a else ""
        if _ss_url:
            _ss_service_text = f'<a href="{_ss_url}" style="{_link_style}">ScoreSaber</a>'
        else:
            _ss_service_text = "ScoreSaber"
        self._ss_star_title_label.setText(f"{_ss_service_text} ／ {_star_title}")
        _bl_url = f"https://beatleader.com/u/{_bl_id_a}" if _bl_id_a else ""
        if _bl_url:
            _bl_service_text = f'<a href="{_bl_url}" style="{_link_style}">BeatLeader</a>'
        else:
            _bl_service_text = "BeatLeader"
        self._bl_star_title_label.setText(f"{_bl_service_text} ／ {_star_title}")
        self._metric_title_label.setText(f"{_star_title}")

        self.table.setHorizontalHeaderLabels([
            "Genre",
            "Metric",
            "A",
            "B",
            "Diff(A⇒B)",
        ])
        # ★テーブル側のヘッダはコンパクトな固定ラベルを使う（サービス名はアイコンで表現）
        self.ss_star_table.setHorizontalHeaderLabels([
            "★",
            "A Clear",
            "B Clear",
            "ΔClear",
            "A FC",
            "B FC",
            "ΔFC",
            "A AvgAcc",
            "B AvgAcc",
            "ΔAcc",
            "★",
            "A PP",
            "B PP",
            "ΔPP",
            "A SPP",
            "B SPP",
            "ΔSPP",
        ])
        self.bl_star_table.setHorizontalHeaderLabels([
            "★",
            "A Clear",
            "B Clear",
            "ΔClear",
            "A FC",
            "B FC",
            "ΔFC",
            "A AvgAcc",
            "B AvgAcc",
            "ΔAcc(L / R)",
            "★",
            "A PP",
            "B PP",
            "ΔPP",
            "A SPP",
            "B SPP",
            "ΔSPP",
        ])

        # ★列ヘッダにサービスアイコンを設定
        ss_head_0 = self.ss_star_table.horizontalHeaderItem(0) or QTableWidgetItem("★")
        ss_head_0.setIcon(self._icon_scoresaber)
        ss_head_0.setToolTip("ScoreSaber")
        self.ss_star_table.setHorizontalHeaderItem(0, ss_head_0)
        ss_head_10 = self.ss_star_table.horizontalHeaderItem(10) or QTableWidgetItem("★")
        ss_head_10.setIcon(self._icon_scoresaber)
        ss_head_10.setToolTip("ScoreSaber")
        self.ss_star_table.setHorizontalHeaderItem(10, ss_head_10)

        bl_head_0 = self.bl_star_table.horizontalHeaderItem(0) or QTableWidgetItem("★")
        bl_head_0.setIcon(self._icon_beatleader)
        bl_head_0.setToolTip("BeatLeader")
        self.bl_star_table.setHorizontalHeaderItem(0, bl_head_0)
        bl_head_10 = self.bl_star_table.horizontalHeaderItem(10) or QTableWidgetItem("★")
        bl_head_10.setIcon(self._icon_beatleader)
        bl_head_10.setToolTip("BeatLeader")
        self.bl_star_table.setHorizontalHeaderItem(10, bl_head_10)

        # 上段: Player / Acc / AccSaber 系の指標

        def _country_flag(code: Optional[str]) -> Optional[str]:
            if not code:
                return None
            cc = str(code).upper()
            if len(cc) != 2 or not cc.isalpha():
                return cc
            base = ord("🇦")
            return chr(base + (ord(cc[0]) - ord("A"))) + chr(base + (ord(cc[1]) - ord("A")))

        def _format_rank_cell(global_rank: Optional[int], country_code: Optional[str], country_rank: Optional[int]) -> Optional[str]:
            if global_rank is None and (country_code is None or country_rank is None):
                return None
            parts: list[str] = []
            if global_rank is not None:
                parts.append(f"{global_rank:,}")
            if country_rank is not None:
                flag = _country_flag(country_code)
                if flag:
                    parts.append(f"({flag} {country_rank:,})")
                else:
                    if country_code:
                        parts.append(f"({country_code} {country_rank:,})")
                    else:
                        parts.append(f"({country_rank:,})")
            return " ".join(parts) if parts else None

        def _set_combined_rank_row(
            row: int,
            label: str,
            global_a: Optional[int],
            country_code_a: Optional[str],
            country_rank_a: Optional[int],
            global_b: Optional[int],
            country_code_b: Optional[str],
            country_rank_b: Optional[int],
            _tbl=None,
        ) -> int:
            _target = _tbl if _tbl is not None else self.table
            text_a = _format_rank_cell(global_a, country_code_a, country_rank_a)
            text_b = _format_rank_cell(global_b, country_code_b, country_rank_b)

            a_value = global_a if global_a is not None else None
            b_value = global_b if global_b is not None else None

            self._set_row(_target, row, label, (a_value, text_a), (b_value, text_b))

            diff_item = _target.item(row, 4)
            if diff_item is not None and isinstance(global_a, (int, float)) and isinstance(global_b, (int, float)):
                diff_global = global_b - global_a
                # Rank 系は数値が小さいほど良いので符号を反転
                diff_global_signed = -diff_global

                diff_text = f"{diff_global_signed:+,d}"

                # 国コードが同じ場合のみ、国別ランク差分を表示する
                if (
                    isinstance(country_rank_a, (int, float))
                    and isinstance(country_rank_b, (int, float))
                    and country_code_a
                    and country_code_b
                    and str(country_code_a).upper() == str(country_code_b).upper()
                ):
                    diff_jp = country_rank_b - country_rank_a
                    diff_jp_signed = -diff_jp
                    flag = _country_flag(str(country_code_a))
                    if flag:
                        diff_text = f"{diff_text} ({flag}{diff_jp_signed:+,d})"

                diff_item.setText(diff_text)
                # _set_row でつけた色をランク方向で上書き（小さいほど良い指標なので反転）
                if diff_global_signed > 0:
                    diff_item.setBackground(diff_positive_bg())
                elif diff_global_signed < 0:
                    diff_item.setBackground(diff_negative_bg())
                else:
                    diff_item.setBackground(diff_neutral_bg())
                diff_item.setForeground(diff_text_color())

            return row + 1

        row_main = 0

        # SS / BL Ranked Play Count の母数（star_stats の map_count 合計）
        _ss_ranked_total_a = sum(s.map_count for s in (snap_a.star_stats or []))
        _ss_ranked_total_b = sum(s.map_count for s in (snap_b.star_stats or []))
        _bl_ranked_total_a = sum(getattr(s, "map_count", 0) for s in (snap_a.beatleader_star_stats or []))
        _bl_ranked_total_b = sum(getattr(s, "map_count", 0) for s in (snap_b.beatleader_star_stats or []))

        def _ranked_play_val(plays: "Optional[int]", total: int) -> "tuple[Optional[int], str] | None":
            """(numeric, 'plays/total') タプルを返す。plays が None なら None。"""
            if plays is None:
                return None
            if total > 0:
                return (plays, f"{plays:,}/{total:,}")
            return (plays, f"{plays:,}")

        # ScoreSaber
        _ss_grp_start = row_main
        self._set_row(self.table, row_main, "[SS] PP", snap_a.scoresaber_pp, snap_b.scoresaber_pp)
        row_main += 1

        row_main = _set_combined_rank_row(
            row_main,
            "[SS] Rank",
            snap_a.scoresaber_rank_global,
            snap_a.scoresaber_country,
            snap_a.scoresaber_rank_country,
            snap_b.scoresaber_rank_global,
            snap_b.scoresaber_country,
            snap_b.scoresaber_rank_country,
        )

        self._set_row(
            self.table,
            row_main,
            "[SS] Avg Ranked Acc",
            (round(snap_a.scoresaber_average_ranked_acc, 2), f"{snap_a.scoresaber_average_ranked_acc:.2f}%") if snap_a.scoresaber_average_ranked_acc is not None else None,
            (round(snap_b.scoresaber_average_ranked_acc, 2), f"{snap_b.scoresaber_average_ranked_acc:.2f}%") if snap_b.scoresaber_average_ranked_acc is not None else None,
        )
        row_main += 1

        self._set_row(
            self.table,
            row_main,
            "[SS] Total Play Count",
            snap_a.scoresaber_total_play_count,
            snap_b.scoresaber_total_play_count,
        )
        row_main += 1

        self._set_row(
            self.table,
            row_main,
            "[SS] Ranked Play Count",
            _ranked_play_val(snap_a.scoresaber_ranked_play_count, _ss_ranked_total_a),
            _ranked_play_val(snap_b.scoresaber_ranked_play_count, _ss_ranked_total_b),
        )
        row_main += 1
        # ScoreSaber グループ: 先頭行に SS アイコンをスパン
        _grp_ss = QTableWidgetItem("")
        _grp_ss.setBackground(label_cell_color())
        _grp_ss.setIcon(self._icon_scoresaber)
        _grp_ss.setToolTip("ScoreSaber")
        self.table.setItem(_ss_grp_start, 0, _grp_ss)
        self.table.setSpan(_ss_grp_start, 0, row_main - _ss_grp_start, 1)

        # BeatLeader
        _bl_grp_start = row_main

        def _format_bl_prestige_value(prestige: "Optional[int]", level: "Optional[int]") -> "tuple[int, str] | None":
            if prestige is None:
                return None
            text = f"{prestige:,}"
            if level is not None:
                text += f" (Lv.{level:,})"
            return (prestige, text)

        self._set_row(
            self.table,
            row_main,
            "[BL] Prestige",
            _format_bl_prestige_value(snap_a.beatleader_prestige, snap_a.beatleader_level),
            _format_bl_prestige_value(snap_b.beatleader_prestige, snap_b.beatleader_level),
        )
        row_main += 1

        self._set_row(self.table, row_main, "[BL] PP", snap_a.beatleader_pp, snap_b.beatleader_pp)
        row_main += 1

        row_main = _set_combined_rank_row(
            row_main,
            "[BL] Rank",
            snap_a.beatleader_rank_global,
            snap_a.beatleader_country,
            snap_a.beatleader_rank_country,
            snap_b.beatleader_rank_global,
            snap_b.beatleader_country,
            snap_b.beatleader_rank_country,
        )

        self._set_row(
            self.table,
            row_main,
            "[BL] Avg Ranked Acc",
            (round(snap_a.beatleader_average_ranked_acc, 2), f"{snap_a.beatleader_average_ranked_acc:.2f}%") if snap_a.beatleader_average_ranked_acc is not None else None,
            (round(snap_b.beatleader_average_ranked_acc, 2), f"{snap_b.beatleader_average_ranked_acc:.2f}%") if snap_b.beatleader_average_ranked_acc is not None else None,
        )
        row_main += 1

        self._set_row(
            self.table,
            row_main,
            "[BL] Total Play Count",
            snap_a.beatleader_total_play_count,
            snap_b.beatleader_total_play_count,
        )
        row_main += 1

        self._set_row(
            self.table,
            row_main,
            "[BL] Ranked Play Count",
            _ranked_play_val(snap_a.beatleader_ranked_play_count, _bl_ranked_total_a),
            _ranked_play_val(snap_b.beatleader_ranked_play_count, _bl_ranked_total_b),
        )
        row_main += 1
        # BeatLeader グループ: 先頭行に BL アイコンをスパン
        _grp_bl = QTableWidgetItem("")
        _grp_bl.setBackground(label_cell_color())
        _grp_bl.setIcon(self._icon_beatleader)
        _grp_bl.setToolTip("BeatLeader")
        self.table.setItem(_bl_grp_start, 0, _grp_bl)
        self.table.setSpan(_bl_grp_start, 0, row_main - _bl_grp_start, 1)

        # AccSaber 用ヘルパー (AS モード時に使用、事前定義)
        def _overall_ap_from_snapshot(snap: Snapshot) -> float | None:
            """True/Standard/Tech の AP 合計を Overall として扱う。

            古いスナップショットで per-skill AP が無い場合だけ、
            保存済みの overall_ap をそのまま使う。
            """
            true_ap = snap.accsaber_true_ap
            standard_ap = snap.accsaber_standard_ap
            tech_ap = snap.accsaber_tech_ap
            if any(v is not None for v in (true_ap, standard_ap, tech_ap)):
                return (true_ap or 0.0) + (standard_ap or 0.0) + (tech_ap or 0.0)
            return snap.accsaber_overall_ap

        def _overall_play_from_snapshot(snap: Snapshot) -> int | None:
            """True/Standard/Tech の Play Count 合計を Overall として扱う。

            古いスナップショットで per-skill のプレイ数が無い場合だけ、
            保存済みの overall_play_count をそのまま使う。
            """
            true_pc = snap.accsaber_true_play_count
            standard_pc = snap.accsaber_standard_play_count
            tech_pc = snap.accsaber_tech_play_count
            if any(v is not None for v in (true_pc, standard_pc, tech_pc)):
                return (true_pc or 0) + (standard_pc or 0) + (tech_pc or 0)
            return snap.accsaber_overall_play_count

        # AccSaber プレイリスト総譜面数（xxx/yyy 表示用）
        try:
            _acc_playlist, _, _ = get_accsaber_playlist_map_counts_from_cache()
        except Exception:  # noqa: BLE001
            _acc_playlist = {}
        _cmp_true_total: Optional[int] = _acc_playlist.get("true")
        _cmp_standard_total: Optional[int] = _acc_playlist.get("standard")
        _cmp_tech_total: Optional[int] = _acc_playlist.get("tech")
        _cmp_parts = [c for c in (_cmp_true_total, _cmp_standard_total, _cmp_tech_total) if c is not None]
        _cmp_overall_total: Optional[int] = sum(_cmp_parts) if _cmp_parts else None

        def _play_fmt(plays: int | None, total: Optional[int]):
            """プレイ数を (数値, 'xxx/yyy') タプルに変換する。total が不明なら数値のみ。"""
            if plays is None:
                return None
            if total is None:
                return plays
            return (plays, f"{plays:,}/{total:,}")

        # Play Count バー用ヘルパー: 設定済みアイテムに ratio と色を付加する
        def _set_play_bar(row: int, plays_a: int | None, plays_b: int | None,
                          total: Optional[int], cat: str) -> None:
            """Play Count 行の col 1/2 に ratio と色を UserRole で設定する。"""
            color = ACC_PLAY_COLORS.get(cat, QColor(128, 128, 128, 160))
            for col, plays in ((2, plays_a), (3, plays_b)):
                item = self.table_acc.item(row, col)
                if item is None or plays is None or total is None or total == 0:
                    continue
                item.setData(Qt.ItemDataRole.UserRole, min(1.0, plays / total))
                item.setData(Qt.ItemDataRole.UserRole + 1, color)

        def _set_avg_acc_bar(row: int, acc_a: "float | None", acc_b: "float | None", cat: str) -> None:
            """Avg Acc 行の col 2/3 に ratio を UserRole で設定する（70–100% スケール、赤→緑グラデーション描画）。"""
            for col, acc in ((2, acc_a), (3, acc_b)):
                item = self.table_acc.item(row, col)
                if item is None or acc is None:
                    continue
                ratio = max(0.0, min(1.0, (acc - 70.0) / 30.0))
                item.setData(Qt.ItemDataRole.UserRole, ratio)
                item.setData(Qt.ItemDataRole.UserRole + 1, True)  # グラデーション描画のセンチネル

        def _set_group_label(start_row: int, count: int, group_text: str) -> None:
            """col 0 にグループラベルをセットし、count 行スパンする。"""
            _icon = self._icon_accsaber if self._acc_mode != "RL" else self._icon_accsaber_rl
            _gi = QTableWidgetItem(group_text)
            _gi.setBackground(label_cell_color())
            _gi.setForeground(label_cell_text_color())
            _gi.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            _gi.setIcon(_icon)
            self.table_acc.setItem(start_row, 0, _gi)
            if count > 1:
                self.table_acc.setSpan(start_row, 0, count, 1)

        # AccSaber 行: self.table_acc に出力 (row_acc = 0 から)
        row_acc = 0
        if self._acc_mode != "RL":
            # AccSaber (AS) モード — 全項目表示 (4 AP + 4 Rank + 4 Play Count + 4 AvgAcc = 16行)
            _grp_start = row_acc
            for _lbl, _attr in (
                ("[AS] Overall",  "accsaber_overall_ap"),
                ("[AS] True",     "accsaber_true_ap"),
                ("[AS] Standard", "accsaber_standard_ap"),
                ("[AS] Tech",     "accsaber_tech_ap"),
            ):
                _v_a = getattr(snap_a, _attr)
                _v_b = getattr(snap_b, _attr)
                self._set_row(self.table_acc, row_acc, _lbl,
                              round(_v_a, 2) if _v_a is not None else None,
                              round(_v_b, 2) if _v_b is not None else None)
                row_acc += 1
            _set_group_label(_grp_start, 4, "AP")
            _grp_start = row_acc
            for _lbl, _r_attr, _rc_attr in (
                ("[AS] Overall",  "accsaber_overall_rank",  "accsaber_overall_rank_country"),
                ("[AS] True",     "accsaber_true_rank",     "accsaber_true_rank_country"),
                ("[AS] Standard", "accsaber_standard_rank", "accsaber_standard_rank_country"),
                ("[AS] Tech",     "accsaber_tech_rank",     "accsaber_tech_rank_country"),
            ):
                row_acc = _set_combined_rank_row(
                    row_acc, _lbl,
                    getattr(snap_a, _r_attr), snap_a.scoresaber_country, getattr(snap_a, _rc_attr),
                    getattr(snap_b, _r_attr), snap_b.scoresaber_country, getattr(snap_b, _rc_attr),
                    _tbl=self.table_acc,
                )
            _set_group_label(_grp_start, 4, "Rank")
            _grp_start = row_acc
            for _lbl, _attr, _cat, _total in (
                ("[AS] Overall",  "accsaber_overall_play_count",  "overall",  _cmp_overall_total),
                ("[AS] True",     "accsaber_true_play_count",     "true",     _cmp_true_total),
                ("[AS] Standard", "accsaber_standard_play_count", "standard", _cmp_standard_total),
                ("[AS] Tech",     "accsaber_tech_play_count",     "tech",     _cmp_tech_total),
            ):
                _pc_a = getattr(snap_a, _attr)
                _pc_b = getattr(snap_b, _attr)
                self._set_row(self.table_acc, row_acc, _lbl, _play_fmt(_pc_a, _total), _play_fmt(_pc_b, _total))
                _set_play_bar(row_acc, _pc_a, _pc_b, _total, _cat)
                row_acc += 1
            _set_group_label(_grp_start, 4, "Play Count")
            _grp_start = row_acc
            for _lbl, _attr, _cat in (
                ("[AS] Overall",  "accsaber_overall_avg_acc",  "overall"),
                ("[AS] True",     "accsaber_true_avg_acc",     "true"),
                ("[AS] Standard", "accsaber_standard_avg_acc", "standard"),
                ("[AS] Tech",     "accsaber_tech_avg_acc",     "tech"),
            ):
                _v_a = getattr(snap_a, _attr)
                _v_b = getattr(snap_b, _attr)
                self._set_row(self.table_acc, row_acc, _lbl,
                              (round(_v_a, 2), f"{_v_a:.2f}%") if _v_a is not None else None,
                              (round(_v_b, 2), f"{_v_b:.2f}%") if _v_b is not None else None)
                _set_avg_acc_bar(row_acc, _v_a, _v_b, _cat)
                row_acc += 1
            _set_group_label(_grp_start, 4, "Acc")
        else:
            # AccSaber Reloaded (RL) モード — 総譜面数をキャッシュから取得
            try:
                _rl_totals = _get_reloaded_map_counts_from_cache()
            except Exception:  # noqa: BLE001
                _rl_totals = {}

            _xp_lv_a = snap_a.accsaber_reloaded_xp_level
            _xp_lv_b = snap_b.accsaber_reloaded_xp_level

            def _xp_val(xp, level):
                if xp is None:
                    return None
                v = int(round(xp))
                if level is not None:
                    return (v, f"{v:,} (Lv.{level})")
                return v

            def _rl_set_xp_row(r: int) -> int:
                self._set_row(
                    self.table_acc, r, "[RL] XP",
                    _xp_val(snap_a.accsaber_reloaded_xp, _xp_lv_a),
                    _xp_val(snap_b.accsaber_reloaded_xp, _xp_lv_b),
                )
                if _xp_lv_a is not None and _xp_lv_b is not None:
                    _lv_diff = _xp_lv_b - _xp_lv_a
                    _diff_item = self.table_acc.item(r, 4)
                    if _diff_item is not None:
                        _diff_item.setText(_diff_item.text() + f" (Lv{_lv_diff:+d})")
                return r + 1

            # 全項目表示 (XP×2 + AP×4 + Rank×4 + Play Count×4 + AvgAcc×4 = 18行)
            _grp_start = row_acc
            row_acc = _rl_set_xp_row(row_acc)
            row_acc = _set_combined_rank_row(
                row_acc, "[RL] Rank",
                snap_a.accsaber_reloaded_xp_rank, snap_a.scoresaber_country, snap_a.accsaber_reloaded_xp_rank_country,
                snap_b.accsaber_reloaded_xp_rank, snap_b.scoresaber_country, snap_b.accsaber_reloaded_xp_rank_country,
                _tbl=self.table_acc,
            )
            _set_group_label(_grp_start, 2, "XP")
            _grp_start = row_acc
            for _lbl, _attr in (
                ("[RL] Overall",  "accsaber_reloaded_overall_ap"),
                ("[RL] True",     "accsaber_reloaded_true_ap"),
                ("[RL] Standard", "accsaber_reloaded_standard_ap"),
                ("[RL] Tech",     "accsaber_reloaded_tech_ap"),
            ):
                _v_a = getattr(snap_a, _attr)
                _v_b = getattr(snap_b, _attr)
                self._set_row(self.table_acc, row_acc, _lbl,
                              round(_v_a, 2) if _v_a is not None else None,
                              round(_v_b, 2) if _v_b is not None else None)
                row_acc += 1
            _set_group_label(_grp_start, 4, "AP")
            _grp_start = row_acc
            for _lbl, _r_attr, _rc_attr in (
                ("[RL] Overall",  "accsaber_reloaded_overall_rank",  "accsaber_reloaded_overall_rank_country"),
                ("[RL] True",     "accsaber_reloaded_true_rank",     "accsaber_reloaded_true_rank_country"),
                ("[RL] Standard", "accsaber_reloaded_standard_rank", "accsaber_reloaded_standard_rank_country"),
                ("[RL] Tech",     "accsaber_reloaded_tech_rank",     "accsaber_reloaded_tech_rank_country"),
            ):
                row_acc = _set_combined_rank_row(
                    row_acc, _lbl,
                    getattr(snap_a, _r_attr), snap_a.scoresaber_country, getattr(snap_a, _rc_attr),
                    getattr(snap_b, _r_attr), snap_b.scoresaber_country, getattr(snap_b, _rc_attr),
                    _tbl=self.table_acc,
                )
            _set_group_label(_grp_start, 4, "Rank")
            _grp_start = row_acc
            for _lbl, _attr, _cat in (
                ("[RL] Overall",  "accsaber_reloaded_overall_ranked_plays",  "overall"),
                ("[RL] True",     "accsaber_reloaded_true_ranked_plays",     "true"),
                ("[RL] Standard", "accsaber_reloaded_standard_ranked_plays", "standard"),
                ("[RL] Tech",     "accsaber_reloaded_tech_ranked_plays",     "tech"),
            ):
                _pc_a = getattr(snap_a, _attr)
                _pc_b = getattr(snap_b, _attr)
                _rl_total = _rl_totals.get(_cat)
                self._set_row(self.table_acc, row_acc, _lbl, _play_fmt(_pc_a, _rl_total), _play_fmt(_pc_b, _rl_total))
                _set_play_bar(row_acc, _pc_a, _pc_b, _rl_total, _cat)
                row_acc += 1
            _set_group_label(_grp_start, 4, "Play Count")
            _grp_start = row_acc
            for _lbl, _attr, _cat in (
                ("[RL] Overall",  "accsaber_reloaded_overall_avg_acc",  "overall"),
                ("[RL] True",     "accsaber_reloaded_true_avg_acc",     "true"),
                ("[RL] Standard", "accsaber_reloaded_standard_avg_acc", "standard"),
                ("[RL] Tech",     "accsaber_reloaded_tech_avg_acc",     "tech"),
            ):
                _v_a = getattr(snap_a, _attr)
                _v_b = getattr(snap_b, _attr)
                self._set_row(self.table_acc, row_acc, _lbl,
                              (round(_v_a, 2), f"{_v_a:.2f}%") if _v_a is not None else None,
                              (round(_v_b, 2), f"{_v_b:.2f}%") if _v_b is not None else None)
                _set_avg_acc_bar(row_acc, _v_a, _v_b, _cat)
                row_acc += 1
            _set_group_label(_grp_start, 4, "Avg Acc")

        # AccSaber 比較グリッドのヘッダを更新
        self.acc_cmp_table.setHorizontalHeaderLabels([
            "", "A AP", "B AP", "\u0394AP",
            "A Rank", "B Rank", "\u0394Rank",
            "A Plays", "B Plays", "\u0394Plays",
            "A Avg Acc", "B Avg Acc", "\u0394Avg Acc",
        ])

        _label_bg = QColor(label_cell_color())
        _label_fg = QColor(label_cell_text_color())

        def _fill_acc_cmp_table(icon: QIcon, rows_a: list, rows_b: list) -> None:
            """AccSaber比較グリッドに A/B データを設定する。

            rows_a/b: list of (cat_name, ap, rank_global, rank_country, country_code,
                                plays, plays_total, avg_acc, cat_key)
            """
            h_item = self.acc_cmp_table.horizontalHeaderItem(0)
            if h_item is None:
                h_item = QTableWidgetItem("")
                self.acc_cmp_table.setHorizontalHeaderItem(0, h_item)
            h_item.setText("")
            h_item.setIcon(icon)

            for row, (row_a, row_b) in enumerate(zip(rows_a, rows_b)):
                cat_name = row_a[0]
                ap_a, rank_g_a, rank_c_a, country_a, plays_a, plays_total_a, avg_acc_a, cat_key_a = row_a[1:]
                ap_b, rank_g_b, rank_c_b, country_b, plays_b, plays_total_b, avg_acc_b, _cat_key_b = row_b[1:]

                # col 0: カテゴリ名（ラベル色）
                item_cat = QTableWidgetItem(cat_name)
                item_cat.setBackground(_label_bg)
                item_cat.setForeground(_label_fg)
                item_cat.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                self.acc_cmp_table.setItem(row, 0, item_cat)

                # col 1/2/3: A AP / B AP / ΔAP
                def _ap_item(ap):
                    it = QTableWidgetItem(f"{round(ap, 2):,.2f}" if ap is not None else "")
                    it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    return it
                self.acc_cmp_table.setItem(row, 1, _ap_item(ap_a))
                self.acc_cmp_table.setItem(row, 2, _ap_item(ap_b))
                delta_ap_item = QTableWidgetItem("")
                delta_ap_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if ap_a is not None and ap_b is not None:
                    d = ap_b - ap_a
                    delta_ap_item.setText(f"{d:+.2f}")
                    if d > 0:
                        delta_ap_item.setBackground(diff_positive_bg())
                    elif d < 0:
                        delta_ap_item.setBackground(diff_negative_bg())
                    else:
                        delta_ap_item.setBackground(diff_neutral_bg())
                    delta_ap_item.setForeground(diff_text_color())
                self.acc_cmp_table.setItem(row, 3, delta_ap_item)

                # col 4/5/6: A Rank / B Rank / ΔRank
                def _rank_text(rank_g, rank_c, country):
                    if rank_g is None:
                        return ""
                    t = f"{rank_g:,}"
                    if rank_c is not None:
                        flag = _country_flag(country)
                        if flag:
                            t += f" ({flag} {rank_c:,})"
                        elif country:
                            t += f" ({country} {rank_c:,})"
                        else:
                            t += f" ({rank_c:,})"
                    return t
                item_ra = QTableWidgetItem(_rank_text(rank_g_a, rank_c_a, country_a))
                item_ra.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.acc_cmp_table.setItem(row, 4, item_ra)
                item_rb = QTableWidgetItem(_rank_text(rank_g_b, rank_c_b, country_b))
                item_rb.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.acc_cmp_table.setItem(row, 5, item_rb)
                delta_rank_item = QTableWidgetItem("")
                delta_rank_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if rank_g_a is not None and rank_g_b is not None:
                    dr = rank_g_a - rank_g_b  # rank は小さいほど良いので逆向き
                    diff_text = f"{dr:+,}"
                    # 国コードが同じ場合に国別ランク差分を追加
                    if (
                        isinstance(rank_c_a, (int, float))
                        and isinstance(rank_c_b, (int, float))
                        and country_a
                        and country_b
                        and str(country_a).upper() == str(country_b).upper()
                    ):
                        diff_country = rank_c_a - rank_c_b
                        flag = _country_flag(str(country_a))
                        if flag:
                            diff_text += f" ({flag}{diff_country:+,d})"
                        else:
                            diff_text += f" ({country_a}{diff_country:+,d})"
                    delta_rank_item.setText(diff_text)
                    if dr > 0:
                        delta_rank_item.setBackground(diff_positive_bg())
                    elif dr < 0:
                        delta_rank_item.setBackground(diff_negative_bg())
                    else:
                        delta_rank_item.setBackground(diff_neutral_bg())
                    delta_rank_item.setForeground(diff_text_color())
                self.acc_cmp_table.setItem(row, 6, delta_rank_item)

                # col 7/8/9: A Play Count / B Play Count / ΔPlay Count（バー付き）
                def _play_item(plays, plays_total, cat_key):
                    if plays is None:
                        return QTableWidgetItem("")
                    play_text = f"{plays:,}/{plays_total:,}" if plays_total else f"{plays:,}"
                    it = QTableWidgetItem(play_text)
                    it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    if plays_total and plays_total > 0:
                        color = ACC_PLAY_COLORS.get(cat_key, QColor(128, 128, 128, 160))
                        it.setData(Qt.ItemDataRole.UserRole, min(1.0, plays / plays_total))
                        it.setData(Qt.ItemDataRole.UserRole + 1, color)
                    return it
                self.acc_cmp_table.setItem(row, 7, _play_item(plays_a, plays_total_a, cat_key_a))
                self.acc_cmp_table.setItem(row, 8, _play_item(plays_b, plays_total_b, cat_key_a))
                delta_play_item = QTableWidgetItem("")
                delta_play_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if plays_a is not None and plays_b is not None:
                    dp = plays_b - plays_a
                    delta_play_item.setText(f"{dp:+,}")
                    if dp > 0:
                        delta_play_item.setBackground(diff_positive_bg())
                    elif dp < 0:
                        delta_play_item.setBackground(diff_negative_bg())
                    else:
                        delta_play_item.setBackground(diff_neutral_bg())
                    delta_play_item.setForeground(diff_text_color())
                self.acc_cmp_table.setItem(row, 9, delta_play_item)

                # col 10/11/12: A Avg Acc / B Avg Acc / ΔAvg Acc（パーセンテージバー）
                def _acc_item(avg_acc):
                    if avg_acc is not None:
                        it = QTableWidgetItem(f"{avg_acc:.2f}%")
                        it.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                        it.setData(Qt.ItemDataRole.UserRole, avg_acc)
                        return it
                    return QTableWidgetItem("")
                self.acc_cmp_table.setItem(row, 10, _acc_item(avg_acc_a))
                self.acc_cmp_table.setItem(row, 11, _acc_item(avg_acc_b))
                delta_acc_item = QTableWidgetItem("")
                delta_acc_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if avg_acc_a is not None and avg_acc_b is not None:
                    da = avg_acc_b - avg_acc_a
                    delta_acc_item.setText(f"{da:+.2f}%")
                    if da > 0:
                        delta_acc_item.setBackground(diff_positive_bg())
                    elif da < 0:
                        delta_acc_item.setBackground(diff_negative_bg())
                    else:
                        delta_acc_item.setBackground(diff_neutral_bg())
                    delta_acc_item.setForeground(diff_text_color())
                self.acc_cmp_table.setItem(row, 12, delta_acc_item)

        # AccSaber / AccSaber Reloaded (モードに応じて切り替え)
        if self._acc_mode == "RL":
            # AccSaber Reloaded 総譜面数をキャッシュから取得
            try:
                _rl_map_counts = _get_reloaded_map_counts_from_cache()
            except Exception:  # noqa: BLE001
                _rl_map_counts = {}

            # グリッドテーブルに AccSaber Reloaded データを設定
            _rl_rows_a = [
                ("Overall",  snap_a.accsaber_reloaded_overall_ap,   snap_a.accsaber_reloaded_overall_rank,   snap_a.accsaber_reloaded_overall_rank_country,   snap_a.scoresaber_country, snap_a.accsaber_reloaded_overall_ranked_plays,  _rl_map_counts.get("overall"),  snap_a.accsaber_reloaded_overall_avg_acc,   "overall"),
                ("True",     snap_a.accsaber_reloaded_true_ap,      snap_a.accsaber_reloaded_true_rank,       snap_a.accsaber_reloaded_true_rank_country,      snap_a.scoresaber_country, snap_a.accsaber_reloaded_true_ranked_plays,     _rl_map_counts.get("true"),     snap_a.accsaber_reloaded_true_avg_acc,      "true"),
                ("Standard", snap_a.accsaber_reloaded_standard_ap,  snap_a.accsaber_reloaded_standard_rank,   snap_a.accsaber_reloaded_standard_rank_country,  snap_a.scoresaber_country, snap_a.accsaber_reloaded_standard_ranked_plays, _rl_map_counts.get("standard"), snap_a.accsaber_reloaded_standard_avg_acc,  "standard"),
                ("Tech",     snap_a.accsaber_reloaded_tech_ap,      snap_a.accsaber_reloaded_tech_rank,       snap_a.accsaber_reloaded_tech_rank_country,      snap_a.scoresaber_country, snap_a.accsaber_reloaded_tech_ranked_plays,     _rl_map_counts.get("tech"),     snap_a.accsaber_reloaded_tech_avg_acc,      "tech"),
            ]
            _rl_rows_b = [
                ("Overall",  snap_b.accsaber_reloaded_overall_ap,   snap_b.accsaber_reloaded_overall_rank,   snap_b.accsaber_reloaded_overall_rank_country,   snap_b.scoresaber_country, snap_b.accsaber_reloaded_overall_ranked_plays,  _rl_map_counts.get("overall"),  snap_b.accsaber_reloaded_overall_avg_acc,   "overall"),
                ("True",     snap_b.accsaber_reloaded_true_ap,      snap_b.accsaber_reloaded_true_rank,       snap_b.accsaber_reloaded_true_rank_country,      snap_b.scoresaber_country, snap_b.accsaber_reloaded_true_ranked_plays,     _rl_map_counts.get("true"),     snap_b.accsaber_reloaded_true_avg_acc,      "true"),
                ("Standard", snap_b.accsaber_reloaded_standard_ap,  snap_b.accsaber_reloaded_standard_rank,   snap_b.accsaber_reloaded_standard_rank_country,  snap_b.scoresaber_country, snap_b.accsaber_reloaded_standard_ranked_plays, _rl_map_counts.get("standard"), snap_b.accsaber_reloaded_standard_avg_acc,  "standard"),
                ("Tech",     snap_b.accsaber_reloaded_tech_ap,      snap_b.accsaber_reloaded_tech_rank,       snap_b.accsaber_reloaded_tech_rank_country,      snap_b.scoresaber_country, snap_b.accsaber_reloaded_tech_ranked_plays,     _rl_map_counts.get("tech"),     snap_b.accsaber_reloaded_tech_avg_acc,      "tech"),
            ]
            _fill_acc_cmp_table(self._icon_accsaber_rl, _rl_rows_a, _rl_rows_b)
        else:
            # --- AccSaber: グリッドテーブルにデータを設定 ---
            overall_ap_a = _overall_ap_from_snapshot(snap_a)
            overall_ap_b = _overall_ap_from_snapshot(snap_b)
            _as_rows_a = [
                ("Overall",  overall_ap_a,                snap_a.accsaber_overall_rank,   snap_a.accsaber_overall_rank_country,   snap_a.scoresaber_country, _overall_play_from_snapshot(snap_a), _cmp_overall_total,  snap_a.accsaber_overall_avg_acc,   "overall"),
                ("True",     snap_a.accsaber_true_ap,     snap_a.accsaber_true_rank,      snap_a.accsaber_true_rank_country,      snap_a.scoresaber_country, snap_a.accsaber_true_play_count,     _cmp_true_total,     snap_a.accsaber_true_avg_acc,      "true"),
                ("Standard", snap_a.accsaber_standard_ap, snap_a.accsaber_standard_rank,  snap_a.accsaber_standard_rank_country,  snap_a.scoresaber_country, snap_a.accsaber_standard_play_count, _cmp_standard_total, snap_a.accsaber_standard_avg_acc,  "standard"),
                ("Tech",     snap_a.accsaber_tech_ap,     snap_a.accsaber_tech_rank,      snap_a.accsaber_tech_rank_country,      snap_a.scoresaber_country, snap_a.accsaber_tech_play_count,     _cmp_tech_total,     snap_a.accsaber_tech_avg_acc,      "tech"),
            ]
            _as_rows_b = [
                ("Overall",  overall_ap_b,                snap_b.accsaber_overall_rank,   snap_b.accsaber_overall_rank_country,   snap_b.scoresaber_country, _overall_play_from_snapshot(snap_b), _cmp_overall_total,  snap_b.accsaber_overall_avg_acc,   "overall"),
                ("True",     snap_b.accsaber_true_ap,     snap_b.accsaber_true_rank,      snap_b.accsaber_true_rank_country,      snap_b.scoresaber_country, snap_b.accsaber_true_play_count,     _cmp_true_total,     snap_b.accsaber_true_avg_acc,      "true"),
                ("Standard", snap_b.accsaber_standard_ap, snap_b.accsaber_standard_rank,  snap_b.accsaber_standard_rank_country,  snap_b.scoresaber_country, snap_b.accsaber_standard_play_count, _cmp_standard_total, snap_b.accsaber_standard_avg_acc,  "standard"),
                ("Tech",     snap_b.accsaber_tech_ap,     snap_b.accsaber_tech_rank,      snap_b.accsaber_tech_rank_country,      snap_b.scoresaber_country, snap_b.accsaber_tech_play_count,     _cmp_tech_total,     snap_b.accsaber_tech_avg_acc,      "tech"),
            ]
            _fill_acc_cmp_table(self._icon_accsaber, _as_rows_a, _as_rows_b)

        # ----- 右側テーブル: ScoreSaber / BeatLeader の総クリア＋★別 -----
        # スナップショットに保存されている★別統計を利用する
        ss_stats_a = snap_a.star_stats or []
        ss_stats_b = snap_b.star_stats or []
        bl_stats_a = snap_a.beatleader_star_stats or []
        bl_stats_b = snap_b.beatleader_star_stats or []

        def _clear_total_value_and_text(stats):
            """総クリア数を数値＋表示文字列のタプルで返す。"""

            if not stats:
                return None
            total_maps = sum(s.map_count for s in stats)
            total_clears = sum(s.clear_count for s in stats)
            if total_maps <= 0:
                text = f"{total_clears} (0.0%)"
            else:
                rate = total_clears / total_maps * 100.0
                text = f"{total_clears} ({rate:.1f}%)"
            return total_clears, text

        ss_clear_total_a = _clear_total_value_and_text(ss_stats_a)
        ss_clear_total_b = _clear_total_value_and_text(ss_stats_b)
        bl_clear_total_a = _clear_total_value_and_text(bl_stats_a)
        bl_clear_total_b = _clear_total_value_and_text(bl_stats_b)

        def _clear_star_value_and_text(stats, star: int):
            """指定★帯のクリア数を数値＋表示文字列のタプルで返す。"""

            for s in stats:
                if getattr(s, "star", None) == star:
                    maps = s.map_count
                    clears = s.clear_count
                    if maps <= 0:
                        text = f"{clears:,} (0.0%)"
                    else:
                        rate = clears / maps * 100.0
                        text = f"{clears:,} ({rate:.1f}%)"
                    return clears, text
            return None

        def _avg_acc_star_value_and_text(stats, star: int):
            """指定★帯の平均精度(%)を数値＋表示文字列のタプルで返す。

            stats が空でなければ（=このスナップはデータ取得済み）、
            ★エントリが存在しない or average_acc が None の場合は (0, "0.00") を返す。
            stats が空の場合のみ None（データ未取得）を返す。
            """

            for s in stats:
                if getattr(s, "star", None) == star:
                    avg = getattr(s, "average_acc", None)
                    return (avg, f"{avg:.2f}") if avg is not None else (0, "0.00")
            return (0, "0.00") if stats else None

        def _avg_acc_left_star_value_and_text(stats, star: int):
            """指定★帯の左手平均精度(%)を数値＋表示文字列のタプルで返す（BL専用）。"""

            for s in stats:
                if getattr(s, "star", None) == star:
                    val = getattr(s, "avg_acc_left", None)
                    if val is None:
                        return None
                    return val, f"{val:.2f}"
            return None

        def _avg_acc_right_star_value_and_text(stats, star: int):
            """指定★帯の右手平均精度(%)を数値＋表示文字列のタプルで返す（BL専用）。"""

            for s in stats:
                if getattr(s, "star", None) == star:
                    val = getattr(s, "avg_acc_right", None)
                    if val is None:
                        return None
                    return val, f"{val:.2f}"
            return None

        def _fc_star_value_and_text(stats, star: int):
            """指定★帯のFC数を数値＋表示文字列のタプルで返す。"""

            for s in stats:
                if getattr(s, "star", None) == star:
                    fc = getattr(s, "fc_count", None)
                    if fc is None:
                        return None  # 未集計
                    maps = s.map_count
                    if maps <= 0:
                        text = f"{fc:,} (0.0%)"
                    else:
                        rate = fc / maps * 100.0
                        text = f"{fc:,} ({rate:.1f}%)"
                    return fc, text
            return None

        def _fc_total_value_and_text(stats):
            """全★帯合計FCを数値＋表示文字列のタプルで返す。"""

            if not stats:
                return None
            if all(getattr(s, "fc_count", None) is None for s in stats):
                return None  # 未集計
            total_maps = sum(s.map_count for s in stats)
            total_fc = sum(getattr(s, "fc_count", None) or 0 for s in stats)
            if total_maps <= 0:
                text = f"{total_fc:,} (0.0%)"
            else:
                rate = total_fc / total_maps * 100.0
                text = f"{total_fc:,} ({rate:.1f}%)"
            return total_fc, text

        def _avg_acc_total_value_and_text(avg_acc: Optional[float]):
            """全体の平均精度(%)を数値＋表示文字列のタプルで返す。"""

            if avg_acc is None:
                return None
            return avg_acc, f"{avg_acc:.2f}"

        def _pp_star_value_and_text(stats, star: int):
            """指定★帯の pp_contribution を数値＋表示文字列のタプルで返す。

            fc_count が設定済みのエントリがある（新フォーマット = PP 集計済み）場合に限り、
            ★エントリなし or pp_contribution が None → (0, "0") を返す。
            旧フォーマット（fc_count がすべて None）なら None を返す。
            """

            new_fmt = any(getattr(s, "fc_count", None) is not None for s in stats)
            for s in stats:
                if getattr(s, "star", None) == star:
                    pp = getattr(s, "pp_contribution", None)
                    if pp is None:
                        return (0, "0") if new_fmt else None
                    return pp, f"{pp:,.0f}"
            return (0, "0") if new_fmt else None

        def _pp_total_value_and_text(stats):
            """全★帯合計 pp_contribution を数値＋表示文字列のタプルで返す。"""

            if not stats:
                return None
            vals = [getattr(s, "pp_contribution", None) for s in stats]
            if all(v is None for v in vals):
                return None
            total_pp = sum(v for v in vals if v is not None)
            return total_pp, f"{total_pp:,.0f}"

        def _pp_solo_star_value_and_text(stats, star: int):
            """指定★帯の pp_solo を数値＋表示文字列のタプルで返す。

            fc_count が設定済みのエントリがある（新フォーマット = PP 集計済み）場合に限り、
            ★エントリなし or pp_solo が None → (0, "0") を返す。
            旧フォーマット（fc_count がすべて None）なら None を返す。
            """

            new_fmt = any(getattr(s, "fc_count", None) is not None for s in stats)
            for s in stats:
                if getattr(s, "star", None) == star:
                    pp = getattr(s, "pp_solo", None)
                    if pp is None:
                        return (0, "0") if new_fmt else None
                    return pp, f"{pp:,.0f}"
            return (0, "0") if new_fmt else None

        def _pp_solo_total_value_and_text(stats):
            """全★帯合計 pp_solo を数値＋表示文字列のタプルで返す。"""

            if not stats:
                return None
            vals = [getattr(s, "pp_solo", None) for s in stats]
            if all(v is None for v in vals):
                return None
            total_pp = sum(v for v in vals if v is not None)
            return total_pp, f"{total_pp:,.0f}"

        def _normalize_pair(value_and_text):
            """(value, text) または None を (numeric, text) 形式に正規化する。"""

            if value_and_text is None:
                return None, ""
            numeric, text = value_and_text
            return numeric, "" if text is None else str(text)

        def _set_star_row(  # type: ignore[name-defined]
            table: QTableWidget,
            row: int,
            label: str,
            clear_a, clear_b,
            avg_a, avg_b,
            avg_left_a=None, avg_left_b=None,
            avg_right_a=None, avg_right_b=None,
            fc_a=None, fc_b=None,
            pp_a=None, pp_b=None,
            pp_solo_a=None, pp_solo_b=None,
        ) -> None:
            """★別テーブルの 1 行分 (Clear + AvgAcc [+ FC [+ PP [+ Solo PP]]]) を設定する。

            avg_left_a/b, avg_right_a/b を渡すと ΔAcc セルに L/R 差分を付加する。
            fc_a/b を渡すと列 7-9 に FC 数と差分を設定する。
            pp_a/b を渡すと列 10-12 に PP 値と差分を設定する。
            pp_solo_a/b を渡すと列 13-15 に Solo PP 値と差分を設定する。
            """

            while table.rowCount() <= row:
                table.insertRow(table.rowCount())

            star_item = QTableWidgetItem(label)
            star_item.setBackground(label_cell_color())
            star_item.setForeground(label_cell_text_color())
            # 右寄せ
            star_item.setTextAlignment(Qt.AlignmentFlag.AlignRight)
            table.setItem(row, 0, star_item)

            a_clear_val, a_clear_text = _normalize_pair(clear_a)
            b_clear_val, b_clear_text = _normalize_pair(clear_b)

            # Clear 数
            table.setItem(row, 1, QTableWidgetItem(a_clear_text))
            table.setItem(row, 2, QTableWidgetItem(b_clear_text))

            # Clear 差分
            diff_clear_item = QTableWidgetItem("")
            diff_clear_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if isinstance(a_clear_val, (int, float)) and isinstance(b_clear_val, (int, float)):
                diff = b_clear_val - a_clear_val
                if isinstance(a_clear_val, float) or isinstance(b_clear_val, float):
                    diff_clear_item.setText(f"{diff:+.2f}")
                else:
                    diff_clear_item.setText(f"{diff:+d}")

                if diff > 0:
                    color = diff_positive_bg()
                elif diff < 0:
                    color = diff_negative_bg()
                else:
                    color = diff_neutral_bg()
                diff_clear_item.setBackground(color)
                diff_clear_item.setForeground(diff_text_color())
            elif a_clear_val is not None or b_clear_val is not None:
                diff_clear_item.setText("-")

            table.setItem(row, 3, diff_clear_item)

            # FC 列 (新配置: 列4-6)
            if fc_a is not None or fc_b is not None:
                a_fc_val, a_fc_text = _normalize_pair(fc_a) if fc_a is not None else (None, "")
                b_fc_val, b_fc_text = _normalize_pair(fc_b) if fc_b is not None else (None, "")
                table.setItem(row, 4, QTableWidgetItem(a_fc_text))
                table.setItem(row, 5, QTableWidgetItem(b_fc_text))

                diff_fc_item = QTableWidgetItem("")
                diff_fc_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if isinstance(a_fc_val, (int, float)) and isinstance(b_fc_val, (int, float)):
                    fc_diff = int(b_fc_val) - int(a_fc_val)
                    diff_fc_item.setText(f"{fc_diff:+d}")
                    if fc_diff > 0:
                        diff_fc_item.setBackground(diff_positive_bg())
                    elif fc_diff < 0:
                        diff_fc_item.setBackground(diff_negative_bg())
                    else:
                        diff_fc_item.setBackground(diff_neutral_bg())
                    diff_fc_item.setForeground(diff_text_color())
                elif a_fc_val is not None or b_fc_val is not None:
                    diff_fc_item.setText("-")
                table.setItem(row, 6, diff_fc_item)

            # AvgAcc (新配置: 列7-9)
            # AvgAcc: 両方 None なら FC 同様にセルを設定しない（空白）
            if avg_a is not None or avg_b is not None:
                a_avg_val, a_avg_text = _normalize_pair(avg_a) if avg_a is not None else (None, "")
                b_avg_val, b_avg_text = _normalize_pair(avg_b) if avg_b is not None else (None, "")
                a_avg_display = (a_avg_text + "%") if isinstance(a_avg_val, (int, float)) else ""
                b_avg_display = (b_avg_text + "%") if isinstance(b_avg_val, (int, float)) else ""
                table.setItem(row, 7, QTableWidgetItem(a_avg_display))
                table.setItem(row, 8, QTableWidgetItem(b_avg_display))

                # ΔAcc (L/R 差分を付加する場合は括弧内に表示)
                diff_acc_item = QTableWidgetItem("")
                diff_acc_item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                if isinstance(a_avg_val, (int, float)) and isinstance(b_avg_val, (int, float)):
                    diff = b_avg_val - a_avg_val
                    diff_text = f"{diff:+.2f}%"

                    # L/R が渡されている場合は括弧内に付加する
                    a_left_val, _ = _normalize_pair(avg_left_a)
                    b_left_val, _ = _normalize_pair(avg_left_b)
                    a_right_val, _ = _normalize_pair(avg_right_a)
                    b_right_val, _ = _normalize_pair(avg_right_b)
                    if isinstance(a_left_val, (int, float)) and isinstance(b_left_val, (int, float)):
                        # 両方に L/R データがある場合 → 差分を表示
                        ar = a_right_val if isinstance(a_right_val, (int, float)) else 0.0
                        br = b_right_val if isinstance(b_right_val, (int, float)) else 0.0
                        ld = b_left_val - a_left_val
                        rd = br - ar
                        diff_text += f"({ld:+.2f}/{rd:+.2f})"

                    diff_acc_item.setText(diff_text)
                    if diff > 0:
                        color = diff_positive_bg()
                    elif diff < 0:
                        color = diff_negative_bg()
                    else:
                        color = diff_neutral_bg()
                    diff_acc_item.setBackground(color)
                    diff_acc_item.setForeground(diff_text_color())
                elif a_avg_val is not None or b_avg_val is not None:
                    diff_acc_item.setText("-")

                table.setItem(row, 9, diff_acc_item)

            # ★列(繰り返し) 刔10 - star_item と同じ内容
            star_repeat = QTableWidgetItem(label)
            star_repeat.setBackground(label_cell_color())
            star_repeat.setForeground(label_cell_text_color())
            star_repeat.setTextAlignment(Qt.AlignmentFlag.AlignRight)
            table.setItem(row, 10, star_repeat)

            # PP 列 (新配置: 刔11-13) — 両方 None なら FC 同様に空白
            if pp_a is not None or pp_b is not None:
                a_pp_val, a_pp_text = _normalize_pair(pp_a) if pp_a is not None else (None, "")
                b_pp_val, b_pp_text = _normalize_pair(pp_b) if pp_b is not None else (None, "")
                table.setItem(row, 11, QTableWidgetItem(a_pp_text))
                table.setItem(row, 12, QTableWidgetItem(b_pp_text))

                diff_pp_item = QTableWidgetItem("")
                diff_pp_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if isinstance(a_pp_val, (int, float)) and isinstance(b_pp_val, (int, float)):
                    pp_diff = b_pp_val - a_pp_val
                    diff_pp_item.setText(f"{pp_diff:+.0f}")
                    if pp_diff > 0:
                        diff_pp_item.setBackground(diff_positive_bg())
                    elif pp_diff < 0:
                        diff_pp_item.setBackground(diff_negative_bg())
                    else:
                        diff_pp_item.setBackground(diff_neutral_bg())
                    diff_pp_item.setForeground(diff_text_color())
                elif a_pp_val is not None or b_pp_val is not None:
                    diff_pp_item.setText("-")
                table.setItem(row, 13, diff_pp_item)

            # Solo PP 列 (新配置: 刔14-16) — 両方 None なら FC 同様に空白
            if pp_solo_a is not None or pp_solo_b is not None:
                a_sp_val, a_sp_text = _normalize_pair(pp_solo_a) if pp_solo_a is not None else (None, "")
                b_sp_val, b_sp_text = _normalize_pair(pp_solo_b) if pp_solo_b is not None else (None, "")
                _a_sp_item = QTableWidgetItem(a_sp_text)
                _a_sp_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, 14, _a_sp_item)
                _b_sp_item = QTableWidgetItem(b_sp_text)
                _b_sp_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, 15, _b_sp_item)

                diff_sp_item = QTableWidgetItem("")
                diff_sp_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if isinstance(a_sp_val, (int, float)) and isinstance(b_sp_val, (int, float)):
                    sp_diff = b_sp_val - a_sp_val
                    diff_sp_item.setText(f"{sp_diff:+.0f}")
                    if sp_diff > 0:
                        diff_sp_item.setBackground(diff_positive_bg())
                    elif sp_diff < 0:
                        diff_sp_item.setBackground(diff_negative_bg())
                    else:
                        diff_sp_item.setBackground(diff_neutral_bg())
                    diff_sp_item.setForeground(diff_text_color())
                elif a_sp_val is not None or b_sp_val is not None:
                    diff_sp_item.setText("-")
                table.setItem(row, 16, diff_sp_item)

        # ScoreSaber 側テーブル
        stars_ss = sorted({s.star for s in ss_stats_a} | {s.star for s in ss_stats_b})
        # ScoreSaber は現在★15が存在しないので、★0〜14 までに限定
        stars_ss = [star for star in stars_ss if star <= 14]

        row_ss = 0
        for star in stars_ss:
            ss_a_clear = _clear_star_value_and_text(ss_stats_a, star)
            ss_b_clear = _clear_star_value_and_text(ss_stats_b, star)
            ss_a_avg = _avg_acc_star_value_and_text(ss_stats_a, star)
            ss_b_avg = _avg_acc_star_value_and_text(ss_stats_b, star)
            ss_a_fc = _fc_star_value_and_text(ss_stats_a, star)
            ss_b_fc = _fc_star_value_and_text(ss_stats_b, star)
            ss_a_pp = _pp_star_value_and_text(ss_stats_a, star)
            ss_b_pp = _pp_star_value_and_text(ss_stats_b, star)
            ss_a_sp = _pp_solo_star_value_and_text(ss_stats_a, star)
            ss_b_sp = _pp_solo_star_value_and_text(ss_stats_b, star)
            _set_star_row(self.ss_star_table, row_ss, str(star), ss_a_clear, ss_b_clear, ss_a_avg, ss_b_avg, fc_a=ss_a_fc, fc_b=ss_b_fc, pp_a=ss_a_pp, pp_b=ss_b_pp, pp_solo_a=ss_a_sp, pp_solo_b=ss_b_sp)
            row_ss += 1

        # Total は一番下に表示
        if ss_clear_total_a is not None or ss_clear_total_b is not None:
            ss_avg_total_a = _avg_acc_total_value_and_text(snap_a.scoresaber_average_ranked_acc)
            ss_avg_total_b = _avg_acc_total_value_and_text(snap_b.scoresaber_average_ranked_acc)
            ss_fc_total_a = _fc_total_value_and_text(ss_stats_a)
            ss_fc_total_b = _fc_total_value_and_text(ss_stats_b)
            ss_pp_total_a = _pp_total_value_and_text(ss_stats_a)
            ss_pp_total_b = _pp_total_value_and_text(ss_stats_b)
            ss_sp_total_a = _pp_solo_total_value_and_text(ss_stats_a)
            ss_sp_total_b = _pp_solo_total_value_and_text(ss_stats_b)
            _set_star_row(self.ss_star_table, row_ss, "Total", ss_clear_total_a, ss_clear_total_b, ss_avg_total_a, ss_avg_total_b, fc_a=ss_fc_total_a, fc_b=ss_fc_total_b, pp_a=ss_pp_total_a, pp_b=ss_pp_total_b, pp_solo_a=ss_sp_total_a, pp_solo_b=ss_sp_total_b)

        # BeatLeader 側テーブル
        stars_bl = sorted({s.star for s in bl_stats_a} | {s.star for s in bl_stats_b})

        row_bl = 0
        for star in stars_bl:
            bl_a_clear = _clear_star_value_and_text(bl_stats_a, star)
            bl_b_clear = _clear_star_value_and_text(bl_stats_b, star)
            bl_a_avg = _avg_acc_star_value_and_text(bl_stats_a, star)
            bl_b_avg = _avg_acc_star_value_and_text(bl_stats_b, star)
            bl_a_left = _avg_acc_left_star_value_and_text(bl_stats_a, star)
            bl_b_left = _avg_acc_left_star_value_and_text(bl_stats_b, star)
            bl_a_right = _avg_acc_right_star_value_and_text(bl_stats_a, star)
            bl_b_right = _avg_acc_right_star_value_and_text(bl_stats_b, star)
            bl_a_fc = _fc_star_value_and_text(bl_stats_a, star)
            bl_b_fc = _fc_star_value_and_text(bl_stats_b, star)
            bl_a_pp = _pp_star_value_and_text(bl_stats_a, star)
            bl_b_pp = _pp_star_value_and_text(bl_stats_b, star)
            bl_a_sp = _pp_solo_star_value_and_text(bl_stats_a, star)
            bl_b_sp = _pp_solo_star_value_and_text(bl_stats_b, star)
            _set_star_row(
                self.bl_star_table, row_bl, str(star),
                bl_a_clear, bl_b_clear, bl_a_avg, bl_b_avg,
                avg_left_a=bl_a_left, avg_left_b=bl_b_left,
                avg_right_a=bl_a_right, avg_right_b=bl_b_right,
                fc_a=bl_a_fc, fc_b=bl_b_fc,
                pp_a=bl_a_pp, pp_b=bl_b_pp,
                pp_solo_a=bl_a_sp, pp_solo_b=bl_b_sp,
            )
            row_bl += 1

        if bl_clear_total_a is not None or bl_clear_total_b is not None:
            bl_avg_total_a = _avg_acc_total_value_and_text(snap_a.beatleader_average_ranked_acc)
            bl_avg_total_b = _avg_acc_total_value_and_text(snap_b.beatleader_average_ranked_acc)
            bl_fc_total_a = _fc_total_value_and_text(bl_stats_a)
            bl_fc_total_b = _fc_total_value_and_text(bl_stats_b)
            bl_pp_total_a = _pp_total_value_and_text(bl_stats_a)
            bl_pp_total_b = _pp_total_value_and_text(bl_stats_b)
            bl_sp_total_a = _pp_solo_total_value_and_text(bl_stats_a)
            bl_sp_total_b = _pp_solo_total_value_and_text(bl_stats_b)
            _set_star_row(
                self.bl_star_table, row_bl, "Total",
                bl_clear_total_a, bl_clear_total_b, bl_avg_total_a, bl_avg_total_b,
                fc_a=bl_fc_total_a, fc_b=bl_fc_total_b,
                pp_a=bl_pp_total_a, pp_b=bl_pp_total_b,
                pp_solo_a=bl_sp_total_a, pp_solo_b=bl_sp_total_b,
            )

        self.table.resizeColumnToContents(0)  # Metric列のみ自動調整、A/B/Diff列は固定幅
        # チェックボックスで設定された列の表示/非表示を再適用
        self._apply_star_col_visibility_inner()
