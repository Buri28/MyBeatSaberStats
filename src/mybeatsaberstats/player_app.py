from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

import json
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QDateTime
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QMessageBox,
    QHeaderView,
    QProgressDialog,
    QStyledItemDelegate,
)

from .snapshot import Snapshot, SNAPSHOT_DIR, BASE_DIR, RESOURCES_DIR, StarClearStat
from .accsaber import AccSaberPlayer, get_accsaber_playlist_map_counts
from .snapshot_view import SnapshotCompareDialog
from .snapshot_graph import SnapshotGraphDialog
from .app import MainWindow as RankingWindow
from .collector.collector import (
    collect_beatleader_star_stats,
    create_snapshot_for_steam_id,
    ensure_global_rank_caches,
    SnapshotOptions,
)
from mybeatsaberstats.collector.map_store import MapStore


class TakeSnapshotDialog(QDialog):
    """スナップショット取得時にSteamIDとデータ取得オプションを選択するダイアログ。"""

    def __init__(self, parent=None, default_steam_id: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Take Snapshot")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)

        # SteamID入力
        form = QFormLayout()
        self._id_edit = QLineEdit(default_steam_id, self)
        form.addRow("SteamID:", self._id_edit)
        layout.addLayout(form)

        # データ取得オプション
        group = QGroupBox("Fetch Options", self)
        group_layout = QVBoxLayout(group)

        self._cb_ss_ranked_maps = QCheckBox("ScoreSaber Ranked Maps", self)
        self._cb_bl_ranked_maps = QCheckBox("BeatLeader Ranked Maps", self)
        self._cb_scoresaber     = QCheckBox("ScoreSaber (Player Info / Scores / Stats)", self)
        self._cb_beatleader     = QCheckBox("BeatLeader (Player Info / Scores / Stats)", self)
        self._cb_accsaber       = QCheckBox("AccSaber (Rank)", self)

        for cb in (
            self._cb_ss_ranked_maps,
            self._cb_bl_ranked_maps,
            self._cb_scoresaber,
            self._cb_beatleader,
            self._cb_accsaber,
        ):
            cb.setChecked(True)
            group_layout.addWidget(cb)

        layout.addWidget(group)

        # スコア取得モード（Fetch ALL / Fetch Until は排他）
        fetch_mode_group = QGroupBox("Score Fetch Mode", self)
        fetch_mode_layout = QFormLayout(fetch_mode_group)

        # --- ScoreSaber ---
        ss_mode_row = QHBoxLayout()
        self._cb_ss_fetch_all = QCheckBox("Fetch ALL (full history)", self)
        self._cb_ss_fetch_all.setChecked(False)
        self._cb_ss_fetch_all.setEnabled(self._cb_scoresaber.isChecked())
        ss_mode_row.addWidget(self._cb_ss_fetch_all)
        ss_mode_row.addSpacing(16)
        self._cb_ss_until = QCheckBox("Until:", self)
        self._cb_ss_until.setChecked(False)
        self._cb_ss_until.setEnabled(self._cb_scoresaber.isChecked())
        self._dt_ss_until = QDateTimeEdit(QDateTime.currentDateTime(), self)
        self._dt_ss_until.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self._dt_ss_until.setCalendarPopup(True)
        self._dt_ss_until.setEnabled(False)
        self._cb_ss_until.toggled.connect(self._dt_ss_until.setEnabled)
        ss_mode_row.addWidget(self._cb_ss_until)
        ss_mode_row.addWidget(self._dt_ss_until, 1)
        fetch_mode_layout.addRow("ScoreSaber:", ss_mode_row)

        # --- BeatLeader ---
        bl_mode_row = QHBoxLayout()
        self._cb_bl_fetch_all = QCheckBox("Fetch ALL (full history)", self)
        self._cb_bl_fetch_all.setChecked(False)
        self._cb_bl_fetch_all.setEnabled(self._cb_beatleader.isChecked())
        bl_mode_row.addWidget(self._cb_bl_fetch_all)
        bl_mode_row.addSpacing(16)
        self._cb_bl_until = QCheckBox("Until:", self)
        self._cb_bl_until.setChecked(False)
        self._cb_bl_until.setEnabled(self._cb_beatleader.isChecked())
        self._dt_bl_until = QDateTimeEdit(QDateTime.currentDateTime(), self)
        self._dt_bl_until.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        self._dt_bl_until.setCalendarPopup(True)
        self._dt_bl_until.setEnabled(False)
        self._cb_bl_until.toggled.connect(self._dt_bl_until.setEnabled)
        bl_mode_row.addWidget(self._cb_bl_until)
        bl_mode_row.addWidget(self._dt_bl_until, 1)
        fetch_mode_layout.addRow("BeatLeader:", bl_mode_row)

        layout.addWidget(fetch_mode_group)

        # 親チェックの ON/OFF に応じてモード行全体を有効/無効化
        def _ss_enabled_toggled(checked: bool) -> None:
            self._cb_ss_fetch_all.setEnabled(checked and not self._cb_ss_until.isChecked())
            self._cb_ss_until.setEnabled(checked and not self._cb_ss_fetch_all.isChecked())
            self._dt_ss_until.setEnabled(checked and self._cb_ss_until.isChecked())

        def _bl_enabled_toggled(checked: bool) -> None:
            self._cb_bl_fetch_all.setEnabled(checked and not self._cb_bl_until.isChecked())
            self._cb_bl_until.setEnabled(checked and not self._cb_bl_fetch_all.isChecked())
            self._dt_bl_until.setEnabled(checked and self._cb_bl_until.isChecked())

        self._cb_scoresaber.toggled.connect(_ss_enabled_toggled)
        self._cb_beatleader.toggled.connect(_bl_enabled_toggled)

        # Fetch ALL と Fetch Until は相互排他
        def _ss_all_toggled(checked: bool) -> None:
            if checked:
                self._cb_ss_until.setChecked(False)
                self._cb_ss_until.setEnabled(False)
                self._dt_ss_until.setEnabled(False)
            else:
                self._cb_ss_until.setEnabled(self._cb_scoresaber.isChecked())

        def _ss_until_toggled(checked: bool) -> None:
            if checked:
                self._cb_ss_fetch_all.setChecked(False)
                self._cb_ss_fetch_all.setEnabled(False)
            else:
                self._cb_ss_fetch_all.setEnabled(self._cb_scoresaber.isChecked())

        def _bl_all_toggled(checked: bool) -> None:
            if checked:
                self._cb_bl_until.setChecked(False)
                self._cb_bl_until.setEnabled(False)
                self._dt_bl_until.setEnabled(False)
            else:
                self._cb_bl_until.setEnabled(self._cb_beatleader.isChecked())

        def _bl_until_toggled(checked: bool) -> None:
            if checked:
                self._cb_bl_fetch_all.setChecked(False)
                self._cb_bl_fetch_all.setEnabled(False)
            else:
                self._cb_bl_fetch_all.setEnabled(self._cb_beatleader.isChecked())

        self._cb_ss_fetch_all.toggled.connect(_ss_all_toggled)
        self._cb_ss_until.toggled.connect(_ss_until_toggled)
        self._cb_bl_fetch_all.toggled.connect(_bl_all_toggled)
        self._cb_bl_until.toggled.connect(_bl_until_toggled)

        # OK / Cancel
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def steam_id(self) -> str:
        return self._id_edit.text().strip()

    def snapshot_options(self) -> SnapshotOptions:
        ss_until: Optional[datetime] = None
        if self._cb_ss_until.isChecked():
            qdt = self._dt_ss_until.dateTime()
            ss_until = datetime(
                qdt.date().year(), qdt.date().month(), qdt.date().day(),
                qdt.time().hour(), qdt.time().minute(), qdt.time().second(),
            )

        bl_until: Optional[datetime] = None
        if self._cb_bl_until.isChecked():
            qdt = self._dt_bl_until.dateTime()
            bl_until = datetime(
                qdt.date().year(), qdt.date().month(), qdt.date().day(),
                qdt.time().hour(), qdt.time().minute(), qdt.time().second(),
            )

        return SnapshotOptions(
            fetch_ss_ranked_maps=self._cb_ss_ranked_maps.isChecked(),
            fetch_bl_ranked_maps=self._cb_bl_ranked_maps.isChecked(),
            fetch_scoresaber=self._cb_scoresaber.isChecked(),
            fetch_beatleader=self._cb_beatleader.isChecked(),
            fetch_accsaber=self._cb_accsaber.isChecked(),
            fetch_ss_star_stats=True,
            fetch_bl_star_stats=True,
            ss_fetch_until=ss_until,
            bl_fetch_until=bl_until,
            ss_ranked_until=ss_until,
            bl_ranked_until=bl_until,
            ss_fetch_all=self._cb_ss_fetch_all.isChecked(),
            bl_fetch_all=self._cb_bl_fetch_all.isChecked(),
        )


class PercentageBarDelegate(QStyledItemDelegate):
    """パーセンテージ値を持つセルに簡易な横棒グラフを描画するデリゲート。

    gradient_min を指定すると、その値以下は常に「0%扱い」（=赤）とし、
    そこから max_value に向けてグラデーションさせる。
    """

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        max_value: float = 100.0,
        gradient_min: float = 0.0,
    ) -> None:
        """ コンストラクタ。
        :param parent: 親ウィジェット
        :param max_value: パーセンテージの最大値（100% に対応する値）
        :param gradient_min: グラデーションの最小値。この値以下は常に 0% 扱いとする。
        """
        super().__init__(parent)
        self._max_value = max_value
        self._min_value = gradient_min

    def paint(self, painter, option, index):  # type: ignore[override]
        """index の値をパーセンテージとして解釈し、横棒グラフを描画する。"""
        value_str = index.data()
        try:
            value = float(str(value_str)) if value_str not in (None, "") else None
        except ValueError:
            value = None

        # 通常描画のみ
        if value is None or not (self._max_value > 0):
            return super().paint(painter, option, index)

        # gradient_min 以下は常に 0（赤）とし、それより上だけを 0-1 に正規化
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

        # 0.0 → 赤, 0.5 → 黄, 0.8 → 緑 → 1.0 のグラデーション
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
            b = int(255 * t/2)
        color = QColor(r, g, b if ratio > 0.8 else 0, 180)

        painter.fillRect(bar_rect, color)
        painter.restore()

        # 100% だけは太字で少しだけ強調する
        is_full = value >= self._max_value - 1e-3

        # 数値テキストを描画（100% のときは太字にする）
        if is_full:
            painter.save()
            font = option.font
            font.setBold(True)
            painter.setFont(font)
            super().paint(painter, option, index)
            painter.restore()
        else:
            super().paint(painter, option, index)


class PlayerWindow(QMainWindow):
    """steamId 単位のランク情報とスナップショットを表示する専用画面。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("My Beat Saber Stats")

        central = QWidget(self)
        layout = QVBoxLayout(central)

        # --- 上部: SteamID 選択 & 操作ボタン ---
        top_row = QHBoxLayout()

        top_row.addWidget(QLabel("Player (from snapshots):"))
        self.player_combo = QComboBox(self)
        top_row.addWidget(self.player_combo, 1)

        # ランク情報キャッシュを取得/更新するボタン
        self.fetch_ranking_button = QPushButton("Fetch Ranking Data")
        self.fetch_ranking_button.clicked.connect(self._fetch_ranking_data)
        top_row.addWidget(self.fetch_ranking_button)

        # スナップショット取得ボタン
        self.snapshot_button = QPushButton("Take Snapshot")
        self.snapshot_button.clicked.connect(self._take_snapshot_for_current_player)
        top_row.addWidget(self.snapshot_button)

        # スナップショット比較 / グラフ表示
        self.compare_button = QPushButton("Snapshot Compare")
        self.compare_button.clicked.connect(self.open_compare)
        top_row.addWidget(self.compare_button)

        self.graph_button = QPushButton("Snapshot Graph")
        self.graph_button.clicked.connect(self.open_graph)
        top_row.addWidget(self.graph_button)

        # ランキング表示ボタン（キャッシュされたランキングJSONから統合ランキングを表示）
        self.ranking_button = QPushButton("Ranking")
        self.ranking_button.clicked.connect(self.open_ranking)
        top_row.addWidget(self.ranking_button)

        # snapshots/ フォルダの再読み込み
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.reload_snapshots)
        top_row.addWidget(self.refresh_button)

        top_row.addStretch(1)
        layout.addLayout(top_row)

        # --- キャッシュ情報行: SS/BL player_scores の最終読み込み日時と総スコア数 ---
        cache_info_row = QHBoxLayout()
        self._ss_cache_label = QLabel("ScoreSaber scores: -")
        self._bl_cache_label = QLabel("BeatLeader scores: -")
        cache_info_row.addWidget(self._ss_cache_label)
        cache_info_row.addSpacing(24)
        cache_info_row.addWidget(self._bl_cache_label)
        cache_info_row.addStretch(1)
        layout.addLayout(cache_info_row)

        # --- 中央〜下部: 3 列レイアウト ---
        # 1 列目: 上段に ScoreSaber/BeatLeader、下段に AccSaber
        # 2 列目: ScoreSaber ★別
        # 3 列目: BeatLeader ★別

        # 1 列目の上段テーブル: ScoreSaber / BeatLeader の各種指標を 1 表にまとめる
        self.main_table = QTableWidget(0, 3, self)
        self.main_table.verticalHeader().setDefaultSectionSize(14)  # 行の高さを少し詰める

        # 1 列目の下段テーブル: AccSaber 用の指標
        self.acc_table = QTableWidget(0, 5, self)
        self.main_table.setHorizontalHeaderLabels(["Metric", "", ""])
        self.acc_table.verticalHeader().setDefaultSectionSize(14)  # 行の高さを少し詰める
        # AccSaber の表であることが分かるよう、ヘッダに明示する
        self.acc_table.setHorizontalHeaderLabels([
            "Metric",
            "Overall",
            "True",
            "Standard",
            "Tech",
        ])

        # 2 列目: ★別クリア統計テーブル（ScoreSaber）
        # SS(スローソング) も未クリア扱いとして別カラムで表示するため、NF/SS の 2 列を用意する。
        self.star_table = QTableWidget(0, 7, self)
        self.star_table.verticalHeader().setDefaultSectionSize(14)  # 行の高さを少し詰める
        self.star_table.setStyleSheet("QTableWidget::item { padding: 0px; margin: 0px; }")
        self.star_table.verticalHeader().setMinimumSectionSize(0)

        self.star_table.setHorizontalHeaderLabels([
            "★",
            "Maps",
            "Clears",
            "Clear Rate (%)",
            "Avg ACC (%)",
            "NF",
            "SS",
        ])

        # 3 列目: BeatLeader 版 ★統計テーブル
        self.bl_star_table = QTableWidget(0, 7, self)
        self.bl_star_table.verticalHeader().setDefaultSectionSize(14)  # 行の高さを少し詰める
        self.bl_star_table.setHorizontalHeaderLabels([
            "★",
            "Maps",
            "Clears",
            "Clear Rate (%)",
            "Avg ACC (%)",
            "NF",
            "SS",
        ])

        # 列幅は内容に合わせて自動調整し、最後の列がレイアウト都合で
        # 不自然に広がらないように stretchLastSection は無効にする
        for table in (self.main_table, self.acc_table, self.star_table, self.bl_star_table):
            header = table.horizontalHeader()
            header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            header.setStretchLastSection(False)

        # サービスごとのアイコンをヘッダに設定
        resources_dir = RESOURCES_DIR
        icon_scoresaber = QIcon(str(resources_dir / "scoresaber_logo.svg"))
        icon_beatleader = QIcon(str(resources_dir / "beatleader_logo.jpg"))
        icon_accsaber = QIcon(str(resources_dir / "asssaber_logo.webp"))

        # 上段メインテーブル: ScoreSaber / BeatLeader 列にアイコンを付与
        ss_header_item = self.main_table.horizontalHeaderItem(1) or QTableWidgetItem("")
        ss_header_item.setIcon(icon_scoresaber)
        ss_header_item.setToolTip("ScoreSaber")
        self.main_table.setHorizontalHeaderItem(1, ss_header_item)

        bl_header_item = self.main_table.horizontalHeaderItem(2) or QTableWidgetItem("")
        bl_header_item.setIcon(icon_beatleader)
        bl_header_item.setToolTip("BeatLeader")
        self.main_table.setHorizontalHeaderItem(2, bl_header_item)

        # AccSaber テーブル: データ列に AccSaber アイコンを付与
        for col in range(1, 5):
            item = self.acc_table.horizontalHeaderItem(col) or QTableWidgetItem("")
            item.setIcon(icon_accsaber)
            item.setToolTip("AccSaber")
            self.acc_table.setHorizontalHeaderItem(col, item)

        # ★テーブル: 先頭列ヘッダにサービスアイコン＋★を表示
        ss_star_header = self.star_table.horizontalHeaderItem(0) or QTableWidgetItem("★")
        ss_star_header.setIcon(icon_scoresaber)
        ss_star_header.setToolTip("ScoreSaber")
        self.star_table.setHorizontalHeaderItem(0, ss_star_header)

        bl_star_header = self.bl_star_table.horizontalHeaderItem(0) or QTableWidgetItem("★")
        bl_star_header.setIcon(icon_beatleader)
        bl_star_header.setToolTip("BeatLeader")
        self.bl_star_table.setHorizontalHeaderItem(0, bl_star_header)

        # ★テーブルは行番号(No.1〜)が紛らわしいので非表示にする
        self.star_table.verticalHeader().setVisible(False)
        self.bl_star_table.verticalHeader().setVisible(False)

        # パーセンテージ列に横棒グラフを表示するデリゲートを適用
        # Clear Rate 用: 0〜100% で赤→黄→緑グラデーション
        perc_clear = PercentageBarDelegate(self, max_value=100.0, gradient_min=0.0)
        # Avg ACC 用: 50% 以下は常に赤、それ以上を 50〜100% の範囲でグラデーション
        perc_acc = PercentageBarDelegate(self, max_value=100.0, gradient_min=50.0)

        # ScoreSaber: Clear Rate (3列目) と Avg ACC (4列目)
        self.star_table.setItemDelegateForColumn(3, perc_clear)

        self.star_table.setItemDelegateForColumn(4, perc_acc)
        # BeatLeader: Clear Rate / Avg ACC
        self.bl_star_table.setItemDelegateForColumn(3, perc_clear)
        self.bl_star_table.setItemDelegateForColumn(4, perc_acc)

        # 1 列目は main_table(上) と acc_table(下) を縦に並べる
        left_splitter = QSplitter(Qt.Orientation.Vertical, self)
        left_splitter.addWidget(self.main_table)
        left_splitter.addWidget(self.acc_table)
        # 上段をやや広めに、下段を少し狭めに取る
        left_splitter.setStretchFactor(0, 35)
        left_splitter.setStretchFactor(1, 30)

        # 全体は 3 列構成: [1 列目] ScoreSaber/BeatLeader + AccSaber, [2 列目] SS ★別, [3 列目] BL ★別
        main_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        main_splitter.addWidget(left_splitter)
        main_splitter.addWidget(self.star_table)
        main_splitter.addWidget(self.bl_star_table)
        # 1 列目をやや広め、2・3 列目を同程度にする
        main_splitter.setStretchFactor(0, 31)
        main_splitter.setStretchFactor(1, 30)
        main_splitter.setStretchFactor(2, 30)

        layout.addWidget(main_splitter, 1)

        self.setCentralWidget(central)

        # データ
        self._snapshots_by_player: Dict[str, List[Snapshot]] = defaultdict(list)
        self._ss_country_by_id: Dict[str, str] = {}
        self._acc_players: List[AccSaberPlayer] = []

        self._load_player_index_countries()
        self._load_accsaber_players()

        # 前回表示していたプレイヤーIDをキャッシュから復元しておく
        self._last_player_id: Optional[str] = self._load_last_player_id()

        self.player_combo.currentIndexChanged.connect(self._on_player_changed)

        self.reload_snapshots()

    # ---------------- internal helpers ----------------

    def _cache_dir(self) -> Path:
        return BASE_DIR / "cache"

    def _settings_path(self) -> Path:
        return self._cache_dir() / "player_window.json"

    def _read_score_cache_meta(self, filename: str) -> Optional[tuple]:
        """キャッシュ JSON から (fetched_at ローカル時刻文字列, total_play_count) を返す。

        ファイルが存在しない、または読み取れない場合は None を返す。
        """
        path = self._cache_dir() / filename
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            fetched_at_str = raw.get("fetched_at")
            total_play_count = raw.get("total_play_count")
            if fetched_at_str is None or total_play_count is None:
                return None
            # UTC → ローカル時刻に変換
            fa = fetched_at_str
            if fa.endswith("Z"):
                fa = fa[:-1]
            dt_utc = datetime.fromisoformat(fa).replace(tzinfo=timezone.utc)
            dt_local = dt_utc.astimezone()
            local_str = dt_local.strftime("%Y-%m-%d %H:%M:%S")
            return (local_str, int(total_play_count))
        except Exception:  # noqa: BLE001
            return None

    def _load_last_player_id(self) -> Optional[str]:
        """前回 Stats 画面で表示していたプレイヤーの SteamID を読み込む。"""

        path = self._settings_path()
        if not path.exists():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

        value = data.get("last_steam_id")
        if isinstance(value, (str, int)):
            s = str(value).strip()
            return s or None
        return None

    def _save_last_player_id(self) -> None:
        """現在選択中のプレイヤーIDをキャッシュに保存する。"""

        steam_id = self._current_player_id()
        if not steam_id:
            return

        path = self._settings_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"last_steam_id": steam_id}
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            # 設定保存に失敗しても画面の動作には影響させない
            return

    def _take_snapshot_for_current_player(self) -> bool:
        """Snapshot 取得時に任意の SteamID と取得オプションを選択できるダイアログを表示する。

        戻り値: スナップショットが正常に作成できたら True、それ以外は False。
        （ボタンから呼ばれる通常利用では戻り値は無視される。）
        """

        # デフォルトは現在選択中のプレイヤーID（なければ空文字）
        current_id = self._current_player_id() or ""

        dlg = TakeSnapshotDialog(self, default_steam_id=current_id)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return False

        steam_id = dlg.steam_id()
        if not steam_id:
            QMessageBox.warning(self, "Take Snapshot", "SteamID is empty.")
            return False

        options = dlg.snapshot_options()

        # スナップショット取得処理の途中でキャンセルできるように、Cancel ボタン付きの
        # QProgressDialog を用意し、キャンセル状態をフラグで管理する。
        cancelled = {"value": False}

        dlg = QProgressDialog("Taking snapshot...", "Cancel", 0, 100, self)
        dlg.setWindowTitle("Take Snapshot")
        dlg.setWindowModality(Qt.WindowModality.WindowModal)
        dlg.setAutoClose(True)
        dlg.canceled.connect(lambda: cancelled.__setitem__("value", True))
        dlg.show()

        def _on_progress(message: str, fraction: float) -> None:
            # キャンセルされていたら、例外を投げて処理全体を中断する
            if cancelled["value"]:
                raise RuntimeError("SNAPSHOT_CANCELLED")
            dlg.setLabelText(message)
            dlg.setValue(int(fraction * 100))
            QApplication.processEvents()

        try:
            # print文は日本語
            print(f"1.スナップショットを取得中: {steam_id}")
            snapshot = create_snapshot_for_steam_id(steam_id, progress=_on_progress, options=options)
            map_store_instance = MapStore()
            map_store_instance.snapshots[steam_id] = snapshot

        except Exception as exc:  # noqa: BLE001
            # キャンセルによる中断の場合はエラーダイアログを出さずに静かに抜ける
            if not cancelled["value"]:
                QMessageBox.warning(self, "Take Snapshot", f"Failed to create snapshot for {steam_id}:\n{exc}")
            return False
        finally:
            dlg.close()

        # スナップショット作成後、一覧を再読み込みしつつ、同じプレイヤーを選択状態に保つ
        self.reload_snapshots()
        for idx in range(self.player_combo.count()):
            data = self.player_combo.itemData(idx)
            if isinstance(data, str) and data == steam_id:
                self.player_combo.setCurrentIndex(idx)
                break

        QMessageBox.information(self, "Take Snapshot", f"Snapshot taken at {snapshot.taken_at} for {steam_id}.")
        return True

    def _collect_star_stats_from_beatleader(self, beatleader_id: Optional[str]) -> List[StarClearStat]:
        """BeatLeader の RankedMap 一覧とプレイヤースコアから★別統計を集計する。"""

        if not beatleader_id:
            return []

        # collector 側の共通実装を利用する
        try:
            stats = collect_beatleader_star_stats(beatleader_id)
        except Exception:  # noqa: BLE001
            stats = []

        return list(stats)

    def _load_player_index_countries(self) -> None:
        """players_index.json から ScoreSaber ID ごとの国コードを読み込む。

        players_index.json に登録されていないプレイヤー（BL-only として登録されているが
        実際は SS にも存在するプレイヤー等）は、scoresaber_ranking.json からも補完する。
        """

        cache_dir = self._cache_dir()
        path = cache_dir / "players_index.json"
        self._ss_country_by_id.clear()

        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    ss = row.get("scoresaber")
                    if not isinstance(ss, dict):
                        continue
                    sid = str(ss.get("id") or "")
                    country = str(ss.get("country") or "").upper()
                    if sid and country:
                        self._ss_country_by_id[sid] = country
            except Exception:  # noqa: BLE001
                pass

        # players_index に無い SS プレイヤーを scoresaber_ranking.json から補完
        # （BL-only として登録されているが実際は SS にも存在するプレイヤー対応）
        for ss_cache in ["scoresaber_ranking.json", "scoresaber_JP.json", "scoresaber_ALL.json"]:
            ss_path = cache_dir / ss_cache
            if not ss_path.exists():
                continue
            try:
                ss_data = json.loads(ss_path.read_text(encoding="utf-8"))
                for item in ss_data:
                    if not isinstance(item, dict):
                        continue
                    sid = str(item.get("id") or "")
                    country = str(item.get("country") or "").upper()
                    if sid and country and sid not in self._ss_country_by_id:
                        self._ss_country_by_id[sid] = country
            except Exception:  # noqa: BLE001
                continue

        # BeatLeader キャッシュからも補完する。
        # BL にしか存在しない（SS キャッシュに未登録の）プレイヤーでも
        # AccSaber に登録されている場合、国コードを特定するために必要。
        # app.py の _populate_table と同じ方針。
        for bl_cache in ["beatleader_ranking.json", "beatleader_JP.json"]:
            bl_path = cache_dir / bl_cache
            if not bl_path.exists():
                continue
            try:
                bl_data = json.loads(bl_path.read_text(encoding="utf-8"))
                for item in bl_data:
                    if not isinstance(item, dict):
                        continue
                    sid = str(item.get("id") or "")
                    country = str(item.get("country") or "").upper()
                    if sid and country and sid not in self._ss_country_by_id:
                        self._ss_country_by_id[sid] = country
            except Exception:  # noqa: BLE001
                continue

    def _load_accsaber_players(self) -> None:
        """AccSaber の overall キャッシュからプレイヤー一覧を読み込む。"""

        path = self._cache_dir() / "accsaber_ranking.json"
        self._acc_players = []

        if not path.exists():
            return

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return

        players: List[AccSaberPlayer] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                players.append(AccSaberPlayer(**item))
            except TypeError:
                continue

        self._acc_players = players

    def _fetch_ranking_data(self) -> None:
        """現在選択中のプレイヤーの国に対するランキングキャッシュを取得する。"""

        steam_id = self._current_player_id()
        if not steam_id:
            QMessageBox.warning(self, "Fetch Ranking Data", "No player selected.")
            return

        progress = QProgressDialog("Fetching ranking data...", "Cancel", 0, 100, self)
        progress.setWindowTitle("Fetch Ranking Data")
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setAutoClose(True)
        progress.setAutoReset(True)
        progress.show()

        cancelled = False

        def _on_progress(message: str, fraction: float) -> None:
            nonlocal cancelled
            if progress.wasCanceled():
                cancelled = True
                raise RuntimeError("RANKING_FETCH_CANCELLED")
            value = int(max(0.0, min(1.0, fraction)) * 100)
            progress.setValue(value)
            progress.setLabelText(message)
            QApplication.processEvents()

        try:
            ensure_global_rank_caches(progress=_on_progress, steam_id=steam_id)
        except RuntimeError as exc:
            if "RANKING_FETCH_CANCELLED" in str(exc):
                # ユーザーキャンセル時は特にメッセージを出さない
                pass
            else:
                QMessageBox.warning(self, "Fetch Ranking Data", f"Failed to fetch ranking data:\n{exc}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Fetch Ranking Data", f"Failed to fetch ranking data:\n{exc}")
        finally:
            progress.close()

    def open_ranking(self) -> None:
        """Ranking ボタン押下時に、Stats 画面で表示中プレイヤーの国籍を使ってランキング画面を開く。"""

        steam_id = self._current_player_id()
        if not steam_id:
            QMessageBox.warning(self, "Ranking", "プレイヤーが選択されていません。")
            return

        # Stats 画面で表示しているプレイヤーの国コードを推定
        country_code = self._current_player_country_code()

        cache_dir = BASE_DIR / "cache"
        ss_path = cache_dir / "scoresaber_ranking.json"
        acc_path = cache_dir / "accsaber_ranking.json"

        # 必要なランキングキャッシュが無ければ案内を出す
        if not ss_path.exists() or not acc_path.exists():
            QMessageBox.warning(
                self,
                "Ranking",
                "ランキングキャッシュが存在しません。\n"
                '先に "Fetch Ranking Data" ボタンでランキングデータを取得してください。',
            )
            return

        # main.py 側のランキング画面(MainWindow)を Stats から開き、現在プレイヤーの行へスクロール
        if not hasattr(self, "_ranking_window") or getattr(self, "_ranking_window", None) is None:
            self._ranking_window = RankingWindow(
                initial_steam_id=steam_id,
                initial_country_code=country_code,
            )
        else:
            win = self._ranking_window
            try:
                # 国選択を反映
                if country_code is None:
                    win.country_combo.setCurrentIndex(0)
                else:
                    matched = False
                    for i in range(win.country_combo.count()):
                        data = win.country_combo.itemData(i)
                        if isinstance(data, str) and data.upper() == country_code:
                            win.country_combo.setCurrentIndex(i)
                            matched = True
                            break
                    # コンボボックスに項目が無い国コードの場合は、編集テキストとして直接設定
                    if not matched and len(country_code) == 2:
                        win.country_combo.setEditText(country_code)
                        # 手動でテーブルを更新
                        win._load_all_caches_for_current_country()  # type: ignore[attr-defined]
                        cc = win._current_country_code()            # type: ignore[attr-defined]
                        win._populate_table(win.acc_players, win.ss_players, cc)  # type: ignore[attr-defined]

                # フォーカス対象のプレイヤーを更新
                win._initial_steam_id = steam_id  # type: ignore[attr-defined]
                win.focus_on_steam_id(steam_id)   # type: ignore[attr-defined]
            except Exception:
                pass

        self._ranking_window.show()

    def _compute_acc_country_ranks(self, scoresaber_id: Optional[str]) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
        """AccSaber Overall / True / Standard / Tech の Country Rank を計算する。

        players_index.json にある ScoreSaber の国コードと、accsaber_ranking.json の AP を使って、
        指定 scoresaber_id の国別順位を算出する。該当データが無ければ None。
        戻り値は (overall, true, standard, tech) のタプル。
        """

        if not scoresaber_id or not self._acc_players or not self._ss_country_by_id:
            return (None, None, None, None)

        country = self._ss_country_by_id.get(scoresaber_id)
        if not country:
            return (None, None, None, None)

        def _parse_ap(text: str) -> float:
            if not text:
                return 0.0
            t = text.replace(",", "")
            import re as _re

            m = _re.search(r"[-+]?\d*\.?\d+", t)
            if not m:
                return 0.0
            try:
                return float(m.group(0))
            except ValueError:
                return 0.0

        # 同一国のプレイヤーだけを集める
        same_country_players: List[AccSaberPlayer] = []
        for p in self._acc_players:
            sid = getattr(p, "scoresaber_id", None)
            if not sid:
                continue
            sid_str = str(sid)
            cc = self._ss_country_by_id.get(sid_str)
            if cc != country:
                continue
            same_country_players.append(p)

        if not same_country_players:
            return (None, None, None, None)

        def _rank_for(get_ap) -> Optional[int]:
            players_sorted = sorted(
                same_country_players,
                key=lambda p: _parse_ap(get_ap(p)),
                reverse=True,
            )
            rank_val = 1
            for p in players_sorted:
                sid = getattr(p, "scoresaber_id", None)
                if str(sid) == scoresaber_id:
                    return rank_val
                rank_val += 1
            return None

        overall_rank = _rank_for(lambda p: getattr(p, "total_ap", ""))
        true_rank = _rank_for(lambda p: getattr(p, "true_ap", ""))
        standard_rank = _rank_for(lambda p: getattr(p, "standard_ap", ""))
        tech_rank = _rank_for(lambda p: getattr(p, "tech_ap", ""))

        return (overall_rank, true_rank, standard_rank, tech_rank)

    def reload_snapshots(self) -> None:
        """snapshots フォルダを読み直して、プレイヤー一覧を更新する。"""
        
        print("■collector.reload_snapshots:スナップショットを再読み込みしています...")
        previous_id = self._current_player_id()
        self._snapshots_by_player.clear()
        self.player_combo.clear()

        if not SNAPSHOT_DIR.exists():
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

        paths = sorted(SNAPSHOT_DIR.glob("*.json"))
        for path in paths:
            try:
                snap = Snapshot.load(path)
            except Exception:
                continue

            self._snapshots_by_player[snap.steam_id].append(snap)

        # 各プレイヤーについて、最新スナップショットの情報をコンボに表示する
        for steam_id, snaps in self._snapshots_by_player.items():
            snaps.sort(key=lambda s: s.taken_at)
            latest = snaps[-1]
            label = f"{latest.scoresaber_name or latest.beatleader_name or ''} ({steam_id})"
            self.player_combo.addItem(label, userData=steam_id)

        # 可能であれば、リロード前に選択していたプレイヤー、または前回起動時のプレイヤーを優先して選択する
        target_id = previous_id or self._last_player_id
        if target_id:
            for idx in range(self.player_combo.count()):
                data = self.player_combo.itemData(idx)
                if isinstance(data, str) and data == target_id:
                    self.player_combo.setCurrentIndex(idx)
                    break
        # 対象が見つからなかった場合は、一覧の最後（最新スナップショットを持つプレイヤー）を選択
        if self.player_combo.count() > 0 and self.player_combo.currentIndex() < 0:
            self.player_combo.setCurrentIndex(self.player_combo.count() - 1)

        if self.player_combo.count() == 0:
            self._update_view()

    def _current_player_id(self) -> Optional[str]:
        idx = self.player_combo.currentIndex()
        if idx < 0:
            return None
        data = self.player_combo.currentData()
        if isinstance(data, str):
            return data
        return None

    def _current_player_country_code(self) -> Optional[str]:
        """Stats 画面で現在選択しているプレイヤーの国コードを推定して返す。"""

        steam_id = self._current_player_id()
        if not steam_id:
            return None

        snaps = self._snapshots_by_player.get(steam_id)
        if snaps:
            snaps_sorted = sorted(snaps, key=lambda s: s.taken_at)
            snap = snaps_sorted[-1]

            # 1. ScoreSaber の国コードを優先
            if snap.scoresaber_country:
                code = str(snap.scoresaber_country).strip().upper()
                if len(code) == 2:
                    return code

            # 2. BeatLeader の国コードをフォールバック
            if snap.beatleader_country:
                code = str(snap.beatleader_country).strip().upper()
                if len(code) == 2:
                    return code

            # 3. players_index から ScoreSaber ID 経由で国コードを引く
            if snap.scoresaber_id:
                sid = str(snap.scoresaber_id)
                cc = self._ss_country_by_id.get(sid)
                if cc and len(cc) == 2:
                    return cc.upper()

        return None

    def _on_player_changed(self, *args) -> None:  # noqa: ANN002, ARG002
        """コンボボックスの選択変更時にビュー更新と選択プレイヤー保存を行う。"""

        self._update_view()
        self._save_last_player_id()

    def _update_view(self) -> None:
        self.main_table.setRowCount(0)
        self.acc_table.setRowCount(0)
        self.star_table.setRowCount(0)
        self.bl_star_table.setRowCount(0)
        steam_id = self._current_player_id()
        if steam_id is None:
            self._ss_cache_label.setText("ScoreSaber scores: -")
            self._bl_cache_label.setText("BeatLeader scores: -")
            return

        snaps = self._snapshots_by_player.get(steam_id)
        if not snaps:
            self._ss_cache_label.setText("ScoreSaber scores: -")
            self._bl_cache_label.setText("BeatLeader scores: -")
            return

        snaps.sort(key=lambda s: s.taken_at)
        snap = snaps[-1]

        # --- キャッシュ情報ラベル更新 ---
        ss_id = snap.scoresaber_id
        bl_id = snap.beatleader_id or steam_id
        ss_meta = self._read_score_cache_meta(f"scoresaber_player_scores_{ss_id}.json") if ss_id else None
        bl_meta = self._read_score_cache_meta(f"beatleader_player_scores_{bl_id}.json")
        if ss_meta:
            self._ss_cache_label.setText(f"SS scores: {ss_meta[1]} maps  (fetched: {ss_meta[0]})")
        else:
            self._ss_cache_label.setText("SS scores: -")
        if bl_meta:
            self._bl_cache_label.setText(f"BL scores: {bl_meta[1]} maps  (fetched: {bl_meta[0]})")
        else:
            self._bl_cache_label.setText("BL scores: -")

        # Snapshot の取得時刻をローカル時刻に変換して表示用文字列を作る
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
        except Exception:  # noqa: BLE001
            taken_text = snap.taken_at

        # ★別統計は ScoreSaber / BeatLeader の Ranked 譜面数にも相当するので、
        # 基本的にはスナップショットに保存された値を使い、無い場合のみ再集計する。
        stats = snap.star_stats or []
        bl_stats = list(snap.beatleader_star_stats or [])
        if not bl_stats:
            bl_stats = self._collect_star_stats_from_beatleader(snap.beatleader_id or snap.steam_id)
        total_ranked_maps = sum(s.map_count for s in stats)

        # ScoreSaber / BeatLeader で対になる指標が一目で分かるよう、
        # 同じ行番号に同じ Metric 名を並べる 1 表構成にする。
        ss_pp_text = f"{snap.scoresaber_pp:.2f}" if snap.scoresaber_pp is not None else None
        bl_pp_text = f"{snap.beatleader_pp:.2f}" if snap.beatleader_pp is not None else None

        ss_acc_text = (
            f"{snap.scoresaber_average_ranked_acc:.2f}"
            if snap.scoresaber_average_ranked_acc is not None
            else None
        )
        bl_acc_text = (
            f"{snap.beatleader_average_ranked_acc:.2f}"
            if snap.beatleader_average_ranked_acc is not None
            else None
        )

        # ScoreSaber Ranked Play Count は「実プレイ数 / Ranked譜面総数」の形式で表示
        ranked_play_ss_text: Optional[str]
        if snap.scoresaber_ranked_play_count is None:
            ranked_play_ss_text = None
        elif total_ranked_maps > 0:
            ranked_play_ss_text = f"{snap.scoresaber_ranked_play_count}/{total_ranked_maps}"
        else:
            ranked_play_ss_text = str(snap.scoresaber_ranked_play_count)

        # BeatLeader Ranked Play Count も同様に / で総数を表示する。
        # BeatLeader 側の「総 Ranked 譜面数」は、BeatLeader★統計から算出した
        # map_count 合計を分母として用いる。
        bl_total_maps_for_ranked = sum(s.map_count for s in bl_stats)
        if snap.beatleader_ranked_play_count is None:
            ranked_play_bl_text = None
        elif bl_total_maps_for_ranked > 0:
            ranked_play_bl_text = f"{snap.beatleader_ranked_play_count}/{bl_total_maps_for_ranked}"
        else:
            ranked_play_bl_text = str(snap.beatleader_ranked_play_count)

        # 国コードから国旗絵文字(🇯🇵など)を生成する
        def _country_flag(code: Optional[str]) -> Optional[str]:
            if not code:
                return None
            cc = str(code).upper()
            if len(cc) != 2 or not cc.isalpha():
                return cc
            base = ord("🇦")  # REGIONAL INDICATOR SYMBOL LETTER A
            return chr(base + (ord(cc[0]) - ord("A"))) + chr(base + (ord(cc[1]) - ord("A")))

        # Name / Country は「Name (🇯🇵)」形式にまとめる
        def _format_name_country(name: Optional[str], country: Optional[str]) -> Optional[str]:
            if not name and not country:
                return None
            flag = _country_flag(country)
            if name and flag:
                return f"{name} ({flag})"
            return name or flag

        ss_name_country = _format_name_country(snap.scoresaber_name, snap.scoresaber_country)
        bl_name_country = _format_name_country(snap.beatleader_name, snap.beatleader_country)

        # Rank 表示は「GlobalRank (🇯🇵 CountryRank)」形式にまとめる
        def _format_rank(global_rank: Optional[int], country: Optional[str], country_rank: Optional[int]) -> Optional[str]:
            if global_rank is None and (not country or country_rank is None):
                return None

            parts: list[str] = []
            if global_rank is not None:
                parts.append(str(global_rank))
            if country and country_rank is not None:
                flag = _country_flag(country)
                if flag:
                    parts.append(f"({flag} {country_rank})")
                else:
                    parts.append(f"({country} {country_rank})")
            return " ".join(parts) if parts else None

        ss_rank_text = _format_rank(
            snap.scoresaber_rank_global,
            snap.scoresaber_country,
            snap.scoresaber_rank_country,
        )
        bl_rank_text = _format_rank(
            snap.beatleader_rank_global,
            snap.beatleader_country,
            snap.beatleader_rank_country,
        )

        # 上段テーブル: Snapshot〜Name/Rank/ACC/Total/Ranked をフル表記で表示する
        metrics = [
            ("Snapshot Time", taken_text, None),
            ("SteamID", snap.steam_id, None),
            ("Name", ss_name_country, bl_name_country),
            ("PP", ss_pp_text, bl_pp_text),
            ("Rank", ss_rank_text, bl_rank_text),
            ("Average Ranked ACC", ss_acc_text, bl_acc_text),
            ("Total Play Count", snap.scoresaber_total_play_count, snap.beatleader_total_play_count),
            ("Ranked Play Count", ranked_play_ss_text, ranked_play_bl_text),
        ]

        for row, (label, ss_value, bl_value) in enumerate(metrics):
            self.main_table.insertRow(row)
            metric_item = QTableWidgetItem(label)
            metric_item.setBackground(QColor(248, 248, 248))
            self.main_table.setItem(row, 0, metric_item)

            ss_text = "" if ss_value is None else str(ss_value)
            self.main_table.setItem(row, 1, QTableWidgetItem(ss_text))

            bl_text = "" if bl_value is None else str(bl_value)
            self.main_table.setItem(row, 2, QTableWidgetItem(bl_text))

        self.main_table.resizeColumnsToContents()

        # AccSaber テーブル（Overall / True / Standard / Tech の Global Rank / Country Rank / PlayCount）
        # Country Rank はスナップショット撮影時点の保存値を使う。
        # 新規スナップショット撮影時にコレクターが正しく計算して保存する。
        overall_country_rank = snap.accsaber_overall_rank_country
        true_country_rank = snap.accsaber_true_rank_country
        standard_country_rank = snap.accsaber_standard_rank_country
        tech_country_rank = snap.accsaber_tech_rank_country

        # AccSaber の Country Rank はプレイヤーの国コードに基づいて表示する。
        # Rank 表示は「GlobalRank (🇨🇦 CountryRank)」のような形式にまとめる。
        acc_country_code: Optional[str] = snap.scoresaber_country or snap.beatleader_country

        def _format_acc_rank(global_rank: Optional[int], country_rank: Optional[int], country_code: Optional[str]) -> Optional[str]:
            if global_rank is None and (country_code is None or country_rank is None):
                return None

            parts: list[str] = []
            if global_rank is not None:
                parts.append(str(global_rank))
            if country_code and country_rank is not None:
                flag = _country_flag(country_code)
                if flag:
                    parts.append(f"({flag} {country_rank})")
                else:
                    parts.append(f"({country_code} {country_rank})")
            return " ".join(parts) if parts else None

        # AccSaber True / Standard / Tech の対象譜面総数を playlist API から取得し、
        # Play Count を「自分のプレイ数 / 総譜面数」の形式で表示する。
        try:
            playlist_counts = get_accsaber_playlist_map_counts()
        except Exception:  # noqa: BLE001
            playlist_counts = {}

        true_total_maps = playlist_counts.get("true")
        standard_total_maps = playlist_counts.get("standard")
        tech_total_maps = playlist_counts.get("tech")
        overall_total_maps: Optional[int]
        parts = [c for c in (true_total_maps, standard_total_maps, tech_total_maps) if c is not None]
        if parts:
            overall_total_maps = sum(parts)
        else:
            overall_total_maps = None

        def _format_play_with_total(plays: Optional[int], total_maps: Optional[int]) -> Optional[str]:
            if plays is None:
                return None
            if total_maps is not None and total_maps > 0:
                return f"{plays}/{total_maps}"
            return str(plays)

        # Snapshot から AP を取得し、True/Standard/Tech の合計を Overall として表示する。
        true_ap = snap.accsaber_true_ap
        standard_ap = snap.accsaber_standard_ap
        tech_ap = snap.accsaber_tech_ap

        if any(v is not None for v in (true_ap, standard_ap, tech_ap)):
            overall_ap = (true_ap or 0.0) + (standard_ap or 0.0) + (tech_ap or 0.0)
        else:
            overall_ap = snap.accsaber_overall_ap

        def _format_ap(value: Optional[float]) -> Optional[str]:
            if value is None:
                return None
            return f"{value:.2f}"

        acc_rows = [
            (
                "AP",
                _format_ap(overall_ap),
                _format_ap(true_ap),
                _format_ap(standard_ap),
                _format_ap(tech_ap),
            ),
            (
                "Rank",
                _format_acc_rank(snap.accsaber_overall_rank, overall_country_rank, acc_country_code),
                _format_acc_rank(snap.accsaber_true_rank, true_country_rank, acc_country_code),
                _format_acc_rank(snap.accsaber_standard_rank, standard_country_rank, acc_country_code),
                _format_acc_rank(snap.accsaber_tech_rank, tech_country_rank, acc_country_code),
            ),
            (
                "Play Count",
                _format_play_with_total(
                    (
                        (snap.accsaber_true_play_count or 0)
                        + (snap.accsaber_standard_play_count or 0)
                        + (snap.accsaber_tech_play_count or 0)
                    )
                    if any(
                        v is not None
                        for v in (
                            snap.accsaber_true_play_count,
                            snap.accsaber_standard_play_count,
                            snap.accsaber_tech_play_count,
                        )
                    )
                    else snap.accsaber_overall_play_count,
                    overall_total_maps,
                ),
                _format_play_with_total(snap.accsaber_true_play_count, true_total_maps),
                _format_play_with_total(snap.accsaber_standard_play_count, standard_total_maps),
                _format_play_with_total(snap.accsaber_tech_play_count, tech_total_maps),
            ),
        ]

        for row, (label, overall, true, standard, tech) in enumerate(acc_rows):
            self.acc_table.insertRow(row)
            metric_item = QTableWidgetItem(label)
            metric_item.setBackground(QColor(248, 248, 248))
            self.acc_table.setItem(row, 0, metric_item)

            overall_text = "" if overall is None else str(overall)
            true_text = "" if true is None else str(true)
            standard_text = "" if standard is None else str(standard)
            tech_text = "" if tech is None else str(tech)

            self.acc_table.setItem(row, 1, QTableWidgetItem(overall_text))
            self.acc_table.setItem(row, 2, QTableWidgetItem(true_text))
            self.acc_table.setItem(row, 3, QTableWidgetItem(standard_text))
            self.acc_table.setItem(row, 4, QTableWidgetItem(tech_text))

        self.acc_table.resizeColumnsToContents()

        # ★別統計（ScoreSaber ベース）と Total 行
        total_maps = 0
        total_clears = 0
        total_nf = 0
        total_ss = 0
        total_clear_rate = 0.0

        for row, s in enumerate(stats):
            self.star_table.insertRow(row)
            star_item = QTableWidgetItem(str(s.star))
            star_item.setBackground(QColor(248, 248, 248))
            # 右寄せ
            star_item.setTextAlignment(Qt.AlignmentFlag.AlignRight)

            self.star_table.setItem(row, 0, star_item)
            self.star_table.setItem(row, 1, QTableWidgetItem(str(s.map_count)))
            # 右寄せ
            item1 = self.star_table.item(row, 1)
            if item1 is not None:
                item1.setTextAlignment(Qt.AlignmentFlag.AlignRight)
            self.star_table.setItem(row, 2, QTableWidgetItem(str(s.clear_count)))
            item2 = self.star_table.item(row, 2)
            if item2 is not None:
                item2.setTextAlignment(Qt.AlignmentFlag.AlignRight)

            percent_text = f"{s.clear_rate * 100:.1f}" if s.map_count > 0 else ""
            self.star_table.setItem(row, 3, QTableWidgetItem(percent_text))

            avg_acc_text = f"{s.average_acc:.2f}" if getattr(s, "average_acc", None) is not None else ""
            self.star_table.setItem(row, 4, QTableWidgetItem(avg_acc_text))

            self.star_table.setItem(row, 5, QTableWidgetItem(str(s.nf_count)))
            item5 = self.star_table.item(row, 5)
            if item5 is not None:
                item5.setTextAlignment(Qt.AlignmentFlag.AlignRight)
            item6 = self.star_table.item(row, 6)
            if item6 is not None:
                item6.setTextAlignment(Qt.AlignmentFlag.AlignRight)

            total_maps += s.map_count
            total_clears += s.clear_count
            total_nf += s.nf_count
            total_ss += s.ss_count

        if stats:
            total_row = self.star_table.rowCount()
            self.star_table.insertRow(total_row)
            total_item = QTableWidgetItem("Total")
            total_item.setBackground(QColor(248, 248, 248))
            self.star_table.setItem(total_row, 0, total_item)
            self.star_table.setItem(total_row, 1, QTableWidgetItem(str(total_maps)))
            self.star_table.setItem(total_row, 2, QTableWidgetItem(str(total_clears)))
            if total_maps > 0:
                total_clear_rate = total_clears / total_maps
                percent_text = f"{total_clear_rate * 100:.1f}"
            else:
                percent_text = ""
            self.star_table.setItem(total_row, 3, QTableWidgetItem(percent_text))

            # Total 行の平均精度は Snapshot 上段で取得している overall の平均精度を表示する
            if snap.scoresaber_average_ranked_acc is not None:
                total_avg_text = f"{snap.scoresaber_average_ranked_acc:.2f}"
            else:
                total_avg_text = ""
            self.star_table.setItem(total_row, 4, QTableWidgetItem(total_avg_text))

            self.star_table.setItem(total_row, 5, QTableWidgetItem(str(total_nf)))
            self.star_table.setItem(total_row, 6, QTableWidgetItem(str(total_ss)))

        self.star_table.resizeColumnsToContents()

        # BeatLeader ★別統計と Total 行
        # BeatLeader 側は BeatLeader の★統計そのものを全て表示する（ScoreSaber に存在しない★15 なども含む）。
        bl_total_maps = 0
        bl_total_clears = 0
        bl_total_nf = 0
        bl_total_ss = 0
        bl_total_clear_rate = 0.0

        for row, s in enumerate(bl_stats):
            self.bl_star_table.insertRow(row)
            star_item = QTableWidgetItem(str(s.star))
            star_item.setBackground(QColor(248, 248, 248))
            # 右寄せ
            star_item.setTextAlignment(Qt.AlignmentFlag.AlignRight)

            self.bl_star_table.setItem(row, 0, star_item)
            self.bl_star_table.setItem(row, 1, QTableWidgetItem(str(s.map_count)))
            item1 = self.bl_star_table.item(row, 1)
            if item1 is not None:
                item1.setTextAlignment(Qt.AlignmentFlag.AlignRight)
            self.bl_star_table.setItem(row, 2, QTableWidgetItem(str(s.clear_count)))
            item2 = self.bl_star_table.item(row, 2)
            if item2 is not None:
                item2.setTextAlignment(Qt.AlignmentFlag.AlignRight)

            percent_text = f"{s.clear_rate * 100:.1f}" if s.map_count > 0 else ""
            self.bl_star_table.setItem(row, 3, QTableWidgetItem(percent_text))

            avg_acc_text = f"{s.average_acc:.2f}" if getattr(s, "average_acc", None) is not None else ""
            self.bl_star_table.setItem(row, 4, QTableWidgetItem(avg_acc_text))

            self.bl_star_table.setItem(row, 5, QTableWidgetItem(str(s.nf_count)))
            item5 = self.bl_star_table.item(row, 5)
            if item5 is not None:
                item5.setTextAlignment(Qt.AlignmentFlag.AlignRight)
            self.bl_star_table.setItem(row, 6, QTableWidgetItem(str(s.ss_count)))
            item6 = self.bl_star_table.item(row, 6)
            if item6 is not None:
                item6.setTextAlignment(Qt.AlignmentFlag.AlignRight)

        # Total 行は bl_stats 全体から集計
        if bl_stats:
            bl_total_maps = sum(s.map_count for s in bl_stats)
            bl_total_clears = sum(s.clear_count for s in bl_stats)
            bl_total_nf = sum(s.nf_count for s in bl_stats)
            bl_total_ss = sum(s.ss_count for s in bl_stats)

        if bl_total_maps > 0:
            bl_total_row = self.bl_star_table.rowCount()
            self.bl_star_table.insertRow(bl_total_row)
            total_item = QTableWidgetItem("Total")
            total_item.setBackground(QColor(248, 248, 248))
            self.bl_star_table.setItem(bl_total_row, 0, total_item)
            self.bl_star_table.setItem(bl_total_row, 1, QTableWidgetItem(str(bl_total_maps)))
            self.bl_star_table.setItem(bl_total_row, 2, QTableWidgetItem(str(bl_total_clears)))
            bl_total_clear_rate = bl_total_clears / bl_total_maps
            percent_text = f"{bl_total_clear_rate * 100:.1f}"
            self.bl_star_table.setItem(bl_total_row, 3, QTableWidgetItem(percent_text))

            if snap.beatleader_average_ranked_acc is not None:
                bl_total_avg_text = f"{snap.beatleader_average_ranked_acc:.2f}"
            else:
                bl_total_avg_text = ""
            self.bl_star_table.setItem(bl_total_row, 4, QTableWidgetItem(bl_total_avg_text))

            self.bl_star_table.setItem(bl_total_row, 5, QTableWidgetItem(str(bl_total_nf)))
            self.bl_star_table.setItem(bl_total_row, 6, QTableWidgetItem(str(bl_total_ss)))

        self.bl_star_table.resizeColumnsToContents()

    def open_compare(self) -> None:
        """スナップショット比較ダイアログを開く。"""

        try:
            steam_id = self._current_player_id()
            dlg = SnapshotCompareDialog(self, initial_steam_id=steam_id)
            dlg.exec()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Snapshot Compare", f"Failed to open snapshot comparer:\n{exc}")

    def open_graph(self) -> None:
        """スナップショットの推移グラフダイアログを開く。"""

        steam_id = self._current_player_id()
        if not steam_id:
            QMessageBox.information(self, "Snapshot Graph", "No player selected.")
            return

        snaps = self._snapshots_by_player.get(steam_id) or []
        if not snaps:
            QMessageBox.information(self, "Snapshot Graph", "No snapshots for this player.")
            return

        try:
            dlg = SnapshotGraphDialog(self, snaps)
            dlg.exec()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Snapshot Graph", f"Failed to open snapshot graph:\n{exc}")


def run() -> None:
    app = QApplication.instance() or QApplication([])
    window = PlayerWindow()
    # ScoreSaber / BeatLeader / AccSaber と★0〜15が見やすいように、やや横長＋縦広めに取る
    window.resize(1100, 560)
    window.show()

    # 起動直後にスナップショットが1つも無い場合は、最初にだけ
    # Take Snapshot ダイアログを表示する。ここでキャンセルされたらそのまま終了する。
    if window.player_combo.count() == 0:
        created = window._take_snapshot_for_current_player()
        if not created:
            return

    app.exec()
