from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from datetime import datetime, timezone
import json
import re
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPalette
from .theme import (
    label_cell_color,
    label_cell_text_color,
    diff_positive_bg,
    diff_negative_bg,
    diff_neutral_bg,
    diff_text_color,
    table_stylesheet,
    is_dark,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGridLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QHeaderView,
    QSplitter,
    QStyledItemDelegate,
)

from .snapshot import Snapshot, SNAPSHOT_DIR, BASE_DIR, RESOURCES_DIR
from .accsaber import get_accsaber_playlist_map_counts


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

    def initStyleOption(self, option, index) -> None:  # type: ignore[override]
        super().initStyleOption(option, index)
        value = self._parse_value(index.data())
        if value is not None and value >= self._max_value - 1e-3:
            option.font.setBold(True)
            option.text = option.text + " 🏆"

    def paint(self, painter, option, index):  # type: ignore[override]
        value = self._parse_value(index.data())

        if value is None or not (self._max_value > 0):
            return super().paint(painter, option, index)

        if value <= self._min_value:
            ratio = 0.0
        else:
            span = self._max_value - self._min_value
            if span <= 0:
                ratio = 0.0
            else:
                ratio = (value - self._min_value) / span
        ratio = max(0.0, min(1.0, ratio))

        painter.save()
        # rect = option.rect.adjusted(2, 2, -2, -2)
        rect = option.rect.adjusted(1, 1, -1, -1)
        bar_width = int(rect.width() * ratio)
        bar_rect = rect.adjusted(0, 0, bar_width - rect.width(), 0)

        if ratio <= 0.5:
            t = ratio / 0.5 if ratio > 0 else 0.0
            r = 255
            g = int(255 * t)
            b = 0
        elif ratio <= 0.8:
            t = (ratio - 0.5) / 0.3
            r = int(255 * (1.0 - t))
            g = 255
            b = 0
        else:
            t = (ratio - 0.8) / 0.2
            r = 0
            g = 255
            b = int(255 * t / 2)
        color = QColor(r, g, b if ratio > 0.8 else 0, 180)

        painter.fillRect(bar_rect, color)
        painter.restore()

        is_full = value >= self._max_value - 1e-3

        # バー色の輝度からテキスト色を決定
        bar_lum = 0.299 * r + 0.587 * g + 0.114 * b
        use_dark_text = ratio >= 0.4 and bar_lum > 140
        dark = is_dark()
        text_color_str = (
            self._dark_text_on_bar if dark else self._light_text_on_bar
        ) if use_dark_text else (
            self._dark_text_off_bar if dark else self._light_text_off_bar
        )
        if text_color_str is not None:
            option.palette.setColor(QPalette.ColorRole.Text, QColor(text_color_str))

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
        self.resize(1360, 680)

        # steam_id ごとにスナップショットを管理する
        self._snapshots_by_player: dict[str, List[Snapshot]] = {}
        # Stats 画面側から渡された「最初に選択しておきたいプレイヤー」
        self._initial_steam_id: Optional[str] = initial_steam_id

        root_layout = QVBoxLayout(self)

        # サービス別アイコン
        resources_dir = RESOURCES_DIR
        self._icon_scoresaber = QIcon(str(resources_dir / "scoresaber_logo.svg"))
        self._icon_beatleader = QIcon(str(resources_dir / "beatleader_logo.jpg"))
        self._icon_accsaber = QIcon(str(resources_dir / "asssaber_logo.webp"))

        # 上部: 左右プレイヤー選択 + それぞれのスナップショット日時選択
        top_grid = QGridLayout()
        top_grid.setAlignment(Qt.AlignmentFlag.AlignLeft)
        top_grid.setHorizontalSpacing(5)

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

        # 左寄せに配置
        root_layout.addLayout(top_grid, Qt.AlignmentFlag.AlignLeft)

        # 下部: 左右3つの比較テーブル（上段系 / ScoreSaber★別 / BeatLeader★別）
        splitter = QSplitter(Qt.Orientation.Horizontal, self)

        # 左: プレイヤー/AccSaber 指標
        self.table = QTableWidget(0, 4, splitter)
        self.table.setStyleSheet(table_stylesheet())
        self.table.verticalHeader().setDefaultSectionSize(14)  # 行の高さを少し詰める
        self.table.verticalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.table.verticalHeader().setMinimumSectionSize(0)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        # Metric列で内容が分かるため行番号は非表示にする
        self.table.verticalHeader().setVisible(False)

        self.table.setHorizontalHeaderLabels([
            "Metric",
            "A",
            "B",
            "Diff (A -> B)",
        ])

        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.resizeSection(0, 150)
        header.resizeSection(3, 70)

        # 中央: ScoreSaber ★別（クリア数 + AvgAcc 比較）
        self.ss_star_table = QTableWidget(0, 7, splitter)
        self.ss_star_table.setStyleSheet(table_stylesheet())
        self.ss_star_table.verticalHeader().setDefaultSectionSize(14)  # 行の高さを少し詰める
        self.ss_star_table.setHorizontalHeaderLabels([
            "★",
            "A Clear",
            "B Clear",
            "dClear",
            "A AvgAcc",
            "B AvgAcc",
            "dAcc",
        ])
        ss_star_header = self.ss_star_table.horizontalHeader()
        ss_star_header.setStretchLastSection(False)
        # ★と差分列などは内容に合わせて、A/B Clear 列は固定幅で少し広めにする
        ss_star_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        ss_star_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        ss_star_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        ss_star_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        ss_star_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        ss_star_header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        ss_star_header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        ss_star_header.resizeSection(0, 70)
        ss_star_header.resizeSection(1, 90)
        ss_star_header.resizeSection(2, 90)
        ss_star_header.resizeSection(3, 70)
        ss_star_header.resizeSection(6, 70)
        # 下段★テーブルは行番号(No)が紛らわしいので非表示にする
        self.ss_star_table.verticalHeader().setVisible(False)

        # 右: BeatLeader ★別（クリア数 + AvgAcc 比較）
        self.bl_star_table = QTableWidget(0, 7, splitter)
        self.bl_star_table.setStyleSheet(table_stylesheet())
        self.bl_star_table.verticalHeader().setDefaultSectionSize(14)  # 行の高さを少し詰める
        self.bl_star_table.setHorizontalHeaderLabels([
            "★",
            "A Clear",
            "B Clear",
            "dClear",
            "A AvgAcc",
            "B AvgAcc",
            "dAcc",
        ])
        bl_star_header = self.bl_star_table.horizontalHeader()
        bl_star_header.setStretchLastSection(False)
        bl_star_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        bl_star_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        bl_star_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        bl_star_header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        bl_star_header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        bl_star_header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        bl_star_header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        bl_star_header.resizeSection(0, 70)
        bl_star_header.resizeSection(1, 90)
        bl_star_header.resizeSection(2, 90)
        bl_star_header.resizeSection(3, 70)
        bl_star_header.resizeSection(6, 70)
        self.bl_star_table.verticalHeader().setVisible(False)

        # パーセンテージ列に横棒グラフを表示するデリゲートを適用
        perc_clear = PercentageBarDelegate(self, max_value=100.0, gradient_min=0.0)
        perc_acc = PercentageBarDelegate(self, max_value=100.0, gradient_min=50.0)

        # Clear 列 (A Clear / B Clear) のカッコ内の % と、AvgAcc 列にバーを表示する。
        # ScoreSaber 側
        self.ss_star_table.setItemDelegateForColumn(1, perc_clear)
        self.ss_star_table.setItemDelegateForColumn(2, perc_clear)
        self.ss_star_table.setItemDelegateForColumn(4, perc_acc)
        self.ss_star_table.setItemDelegateForColumn(5, perc_acc)

        # BeatLeader 側
        self.bl_star_table.setItemDelegateForColumn(1, perc_clear)
        self.bl_star_table.setItemDelegateForColumn(2, perc_clear)
        self.bl_star_table.setItemDelegateForColumn(4, perc_acc)
        self.bl_star_table.setItemDelegateForColumn(5, perc_acc)

        root_layout.addWidget(splitter, 1)
        # デフォルトの分割比率
        splitter.setSizes([325, 350, 350])

        self._load_snapshots()
        # Stats 画面から steam_id が渡されている場合はそちらを優先し、
        # そのプレイヤーについて「最後に選択していたスナップショット日付」を復元する。
        # steam_id が渡されていない場合のみ、従来通りダイアログ全体の前回状態を復元する。
        if self._initial_steam_id is None:
            self._restore_last_selection()
        else:
            self._restore_last_selection_for_player(self._initial_steam_id)

        self.combo_player_a.currentIndexChanged.connect(self._on_player_a_changed)
        self.combo_player_b.currentIndexChanged.connect(self._on_player_b_changed)
        self.combo_a.currentIndexChanged.connect(self._on_snapshot_a_changed)
        self.combo_b.currentIndexChanged.connect(self._on_snapshot_b_changed)
        self.button_latest_b.clicked.connect(self._on_select_latest_b)

        self._update_view2()

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

    def _save_last_selection(self) -> None:
        """現在のプレイヤー/スナップショット選択状態を設定ファイルに保存する。"""

        snap_a = self._current_snapshot(self.combo_a)
        snap_b = self._current_snapshot(self.combo_b)

        if snap_a is None and snap_b is None:
            return

        path = self._settings_path()
        try:
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    data = {}
            else:
                data = {}

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

        item0 = QTableWidgetItem(text)
        item0.setBackground(label_cell_color())
        item0.setForeground(label_cell_text_color())
        if icon is not None:
            item0.setIcon(icon)
            item0.setToolTip(original_label)

        table.setItem(row, 0, item0)

        def _extract(value):
            # (numeric_value, display_text) 形式を解釈する
            if isinstance(value, tuple) and len(value) == 2:
                numeric, display = value
                text = "" if display is None else str(display)
                return numeric, text
            text = "" if value is None else str(value)
            return value, text

        a_numeric, text_a = _extract(a)
        b_numeric, text_b = _extract(b)

        table.setItem(row, 1, QTableWidgetItem(text_a))
        table.setItem(row, 2, QTableWidgetItem(text_b))

        diff_item = QTableWidgetItem("")

        # 数値同士なら差分を計算して色を付ける
        if isinstance(a_numeric, (int, float)) and isinstance(b_numeric, (int, float)):
            # Rank 系の指標は「数値が小さいほど良い」ので符号を反転させる。
            # 例: ランクが 1000 → 900 に改善した場合、+100 として扱う。
            is_rank_metric = ("Rank" in label and "Ranked" not in label)

            diff = b_numeric - a_numeric
            if is_rank_metric:
                diff = -diff
            if isinstance(a_numeric, float) or isinstance(b_numeric, float):
                diff_item.setText(f"{diff:+.2f}")
            else:
                diff_item.setText(f"{diff:+d}")

            if diff > 0:
                color = diff_positive_bg()
            elif diff < 0:
                color = diff_negative_bg()
            else:
                color = diff_neutral_bg()
            diff_item.setBackground(color)
            diff_item.setForeground(diff_text_color())

        table.setItem(row, 3, diff_item)

    def _update_view2(self) -> None:
        """スナップショット比較テーブルを更新する（新実装）。"""

        snap_a = self._current_snapshot(self.combo_a)
        snap_b = self._current_snapshot(self.combo_b)
        self.table.setRowCount(0)
        self.ss_star_table.setRowCount(0)
        self.bl_star_table.setRowCount(0)

        if snap_a is None or snap_b is None:
            return

        # A / B 列ヘッダにスナップショット日付を含める（例: A (2026/01/11)）
        def _date_only(taken_at: str) -> str:
            try:
                t_str = taken_at
                if t_str.endswith("Z"):
                    t_str = t_str[:-1]
                    dt_utc = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
                else:
                    dt_utc = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
                dt_local = dt_utc.astimezone()
                return dt_local.strftime("%Y/%m/%d")
            except Exception:
                return taken_at

        date_a = _date_only(snap_a.taken_at)
        date_b = _date_only(snap_b.taken_at)
        self.table.setHorizontalHeaderLabels([
            "Metric",
            f"A ({date_a})",
            f"B ({date_b})",
            "Diff (A -> B)",
        ])
        # ★テーブル側のヘッダはコンパクトな固定ラベルを使う（サービス名はアイコンで表現）
        self.ss_star_table.setHorizontalHeaderLabels([
            "★",
            "A Clear",
            "B Clear",
            "ΔClear",
            "A AvgAcc",
            "B AvgAcc",
            "ΔAcc",
        ])
        self.bl_star_table.setHorizontalHeaderLabels([
            "★",
            "A Clear",
            "B Clear",
            "ΔClear",
            "A AvgAcc",
            "B AvgAcc",
            "ΔAcc",
        ])

        # ★列ヘッダにサービスアイコンを設定
        ss_head = self.ss_star_table.horizontalHeaderItem(0) or QTableWidgetItem("★")
        ss_head.setIcon(self._icon_scoresaber)
        ss_head.setToolTip("ScoreSaber")
        self.ss_star_table.setHorizontalHeaderItem(0, ss_head)

        bl_head = self.bl_star_table.horizontalHeaderItem(0) or QTableWidgetItem("★")
        bl_head.setIcon(self._icon_beatleader)
        bl_head.setToolTip("BeatLeader")
        self.bl_star_table.setHorizontalHeaderItem(0, bl_head)

        # 上段: Player / ACC / AccSaber 系の指標

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
                parts.append(str(global_rank))
            if country_rank is not None:
                flag = _country_flag(country_code)
                if flag:
                    parts.append(f"({flag} {country_rank})")
                else:
                    if country_code:
                        parts.append(f"({country_code} {country_rank})")
                    else:
                        parts.append(f"({country_rank})")
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
        ) -> int:
            text_a = _format_rank_cell(global_a, country_code_a, country_rank_a)
            text_b = _format_rank_cell(global_b, country_code_b, country_rank_b)

            a_value = global_a if global_a is not None else None
            b_value = global_b if global_b is not None else None

            self._set_row(self.table, row, label, (a_value, text_a), (b_value, text_b))

            diff_item = self.table.item(row, 3)
            if diff_item is not None and isinstance(global_a, (int, float)) and isinstance(global_b, (int, float)):
                diff_global = global_b - global_a
                # Rank 系は数値が小さいほど良いので符号を反転
                diff_global_signed = -diff_global

                diff_text = f"{diff_global_signed:+d}"

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
                        diff_text = f"{diff_text} ({flag}{diff_jp_signed:+d})"

                diff_item.setText(diff_text)

            return row + 1

        row_main = 0

        # ScoreSaber
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
            "[SS] Average Ranked ACC",
            round(snap_a.scoresaber_average_ranked_acc, 2) if snap_a.scoresaber_average_ranked_acc is not None else None,
            round(snap_b.scoresaber_average_ranked_acc, 2) if snap_b.scoresaber_average_ranked_acc is not None else None,
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
            snap_a.scoresaber_ranked_play_count,
            snap_b.scoresaber_ranked_play_count,
        )
        row_main += 1

        # BeatLeader
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
            "[BL] Average Ranked ACC",
            round(snap_a.beatleader_average_ranked_acc, 2) if snap_a.beatleader_average_ranked_acc is not None else None,
            round(snap_b.beatleader_average_ranked_acc, 2) if snap_b.beatleader_average_ranked_acc is not None else None,
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
            snap_a.beatleader_ranked_play_count,
            snap_b.beatleader_ranked_play_count,
        )
        row_main += 1

        # AccSaber AP
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

        overall_ap_a = _overall_ap_from_snapshot(snap_a)
        overall_ap_b = _overall_ap_from_snapshot(snap_b)

        self._set_row(
            self.table,
            row_main,
            "[AS] Overall AP",
            (round(overall_ap_a, 2) if overall_ap_a is not None else None),
            (round(overall_ap_b, 2) if overall_ap_b is not None else None),
        )
        row_main += 1

        self._set_row(
            self.table,
            row_main,
            "[AS] True AP",
            (round(snap_a.accsaber_true_ap, 2) if snap_a.accsaber_true_ap is not None else None),
            (round(snap_b.accsaber_true_ap, 2) if snap_b.accsaber_true_ap is not None else None),
        )
        row_main += 1

        self._set_row(
            self.table,
            row_main,
            "[AS] Standard AP",
            (round(snap_a.accsaber_standard_ap, 2) if snap_a.accsaber_standard_ap is not None else None),
            (round(snap_b.accsaber_standard_ap, 2) if snap_b.accsaber_standard_ap is not None else None),
        )
        row_main += 1

        self._set_row(
            self.table,
            row_main,
            "[AS] Tech AP",
            (round(snap_a.accsaber_tech_ap, 2) if snap_a.accsaber_tech_ap is not None else None),
            (round(snap_b.accsaber_tech_ap, 2) if snap_b.accsaber_tech_ap is not None else None),
        )
        row_main += 1

        # AccSaber Rank (Global + Country を 1 行にまとめる)
        # Country Rank はスナップショット撮影時に計算した保存値をそのまま使う。
        # キャッシュを再計算すると snap_a / snap_b が「同じ現時点のランク」を参照してしまい
        # 比較が無意味になるため。
        row_main = _set_combined_rank_row(
            row_main,
            "[AS] Overall Rank",
            snap_a.accsaber_overall_rank,
            snap_a.scoresaber_country,
            snap_a.accsaber_overall_rank_country,
            snap_b.accsaber_overall_rank,
            snap_b.scoresaber_country,
            snap_b.accsaber_overall_rank_country,
        )

        row_main = _set_combined_rank_row(
            row_main,
            "[AS] True Rank",
            snap_a.accsaber_true_rank,
            snap_a.scoresaber_country,
            snap_a.accsaber_true_rank_country,
            snap_b.accsaber_true_rank,
            snap_b.scoresaber_country,
            snap_b.accsaber_true_rank_country,
        )

        row_main = _set_combined_rank_row(
            row_main,
            "[AS] Standard Rank",
            snap_a.accsaber_standard_rank,
            snap_a.scoresaber_country,
            snap_a.accsaber_standard_rank_country,
            snap_b.accsaber_standard_rank,
            snap_b.scoresaber_country,
            snap_b.accsaber_standard_rank_country,
        )

        row_main = _set_combined_rank_row(
            row_main,
            "[AS] Tech Rank",
            snap_a.accsaber_tech_rank,
            snap_a.scoresaber_country,
            snap_a.accsaber_tech_rank_country,
            snap_b.accsaber_tech_rank,
            snap_b.scoresaber_country,
            snap_b.accsaber_tech_rank_country,
        )

        # AccSaber Play Count
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
            _acc_playlist = get_accsaber_playlist_map_counts()
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
            return (plays, f"{plays}/{total}")

        self._set_row(
            self.table,
            row_main,
            "[AS] Overall Play Count",
            _play_fmt(_overall_play_from_snapshot(snap_a), _cmp_overall_total),
            _play_fmt(_overall_play_from_snapshot(snap_b), _cmp_overall_total),
        )
        row_main += 1

        self._set_row(
            self.table,
            row_main,
            "[AS] True Play Count",
            _play_fmt(snap_a.accsaber_true_play_count, _cmp_true_total),
            _play_fmt(snap_b.accsaber_true_play_count, _cmp_true_total),
        )
        row_main += 1

        self._set_row(
            self.table,
            row_main,
            "[AS] Standard Play Count",
            _play_fmt(snap_a.accsaber_standard_play_count, _cmp_standard_total),
            _play_fmt(snap_b.accsaber_standard_play_count, _cmp_standard_total),
        )
        row_main += 1

        self._set_row(
            self.table,
            row_main,
            "[AS] Tech Play Count",
            _play_fmt(snap_a.accsaber_tech_play_count, _cmp_tech_total),
            _play_fmt(snap_b.accsaber_tech_play_count, _cmp_tech_total),
        )
        row_main += 1

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
                        text = f"{clears} (0.0%)"
                    else:
                        rate = clears / maps * 100.0
                        text = f"{clears} ({rate:.1f}%)"
                    return clears, text
            return None

        def _avg_acc_star_value_and_text(stats, star: int):
            """指定★帯の平均精度(%)を数値＋表示文字列のタプルで返す。"""

            for s in stats:
                if getattr(s, "star", None) == star:
                    avg = getattr(s, "average_acc", None)
                    if avg is None:
                        return None
                    return avg, f"{avg:.2f}"
            return None

        def _avg_acc_total_value_and_text(avg_acc: Optional[float]):
            """全体の平均精度(%)を数値＋表示文字列のタプルで返す。"""

            if avg_acc is None:
                return None
            return avg_acc, f"{avg_acc:.2f}"

        def _normalize_pair(value_and_text):
            """(value, text) または None を (numeric, text) 形式に正規化する。"""

            if value_and_text is None:
                return None, ""
            numeric, text = value_and_text
            return numeric, "" if text is None else str(text)

        def _set_star_row(table: QTableWidget, row: int, label: str,  # type: ignore[name-defined]
                          clear_a, clear_b, avg_a, avg_b) -> None:
            """★別テーブルの 1 行分 (Clear + AvgAcc) を設定する。"""

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
            a_avg_val, a_avg_text = _normalize_pair(avg_a)
            b_avg_val, b_avg_text = _normalize_pair(avg_b)

            # Clear 数
            table.setItem(row, 1, QTableWidgetItem(a_clear_text))
            table.setItem(row, 2, QTableWidgetItem(b_clear_text))

            # Clear 差分
            diff_clear_item = QTableWidgetItem("")
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

            table.setItem(row, 3, diff_clear_item)

            # AvgAcc
            if a_avg_text == "":
                a_avg_text = "0.00"
            if b_avg_text == "":
                b_avg_text = "0.00"
            table.setItem(row, 4, QTableWidgetItem(a_avg_text + "%"))
            table.setItem(row, 5, QTableWidgetItem(b_avg_text + "%"))

            # AvgAcc 差分
            diff_acc_item = QTableWidgetItem("0.00%")
            if isinstance(a_avg_val, (int, float)) and isinstance(b_avg_val, (int, float)):
                diff = b_avg_val - a_avg_val
                diff_acc_item.setText(f"{diff:+.2f}%")

                if diff > 0:
                    color = diff_positive_bg()
                elif diff < 0:
                    color = diff_negative_bg()
                else:
                    color = diff_neutral_bg()
                diff_acc_item.setBackground(color)
                diff_acc_item.setForeground(diff_text_color())

            table.setItem(row, 6, diff_acc_item)

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
            _set_star_row(self.ss_star_table, row_ss, str(star), ss_a_clear, ss_b_clear, ss_a_avg, ss_b_avg)
            row_ss += 1

        # Total は一番下に表示
        if ss_clear_total_a is not None or ss_clear_total_b is not None:
            ss_avg_total_a = _avg_acc_total_value_and_text(snap_a.scoresaber_average_ranked_acc)
            ss_avg_total_b = _avg_acc_total_value_and_text(snap_b.scoresaber_average_ranked_acc)
            _set_star_row(self.ss_star_table, row_ss, "Total", ss_clear_total_a, ss_clear_total_b, ss_avg_total_a, ss_avg_total_b)

        # BeatLeader 側テーブル
        stars_bl = sorted({s.star for s in bl_stats_a} | {s.star for s in bl_stats_b})

        row_bl = 0
        for star in stars_bl:
            bl_a_clear = _clear_star_value_and_text(bl_stats_a, star)
            bl_b_clear = _clear_star_value_and_text(bl_stats_b, star)
            bl_a_avg = _avg_acc_star_value_and_text(bl_stats_a, star)
            bl_b_avg = _avg_acc_star_value_and_text(bl_stats_b, star)
            _set_star_row(self.bl_star_table, row_bl, str(star), bl_a_clear, bl_b_clear, bl_a_avg, bl_b_avg)
            row_bl += 1

        if bl_clear_total_a is not None or bl_clear_total_b is not None:
            bl_avg_total_a = _avg_acc_total_value_and_text(snap_a.beatleader_average_ranked_acc)
            bl_avg_total_b = _avg_acc_total_value_and_text(snap_b.beatleader_average_ranked_acc)
            _set_star_row(self.bl_star_table, row_bl, "Total", bl_clear_total_a, bl_clear_total_b, bl_avg_total_a, bl_avg_total_b)

        self.table.resizeColumnsToContents()
