import sys
import json
import re
from dataclasses import asdict
from pathlib import Path
from typing import Optional, Dict

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QColor, QBrush
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QMessageBox,
    QProgressDialog,
)

from .accsaber import (
    AccSaberPlayer,
    fetch_overall,
    fetch_true,
    fetch_standard,
    fetch_tech,
    ACCSABER_MIN_AP_GLOBAL,
    ACCSABER_MIN_AP_SKILL,
)
from .scoresaber import ScoreSaberPlayer, fetch_players
from .beatleader import BeatLeaderPlayer, fetch_players_ranking
from .collector import rebuild_player_index_from_global, ensure_global_rank_caches
from .snapshot import BASE_DIR, resource_path


# キャッシュは常に「プロジェクトルート / exe のあるディレクトリ」配下の cache を使う
CACHE_DIR = BASE_DIR / "cache"


SCORESABER_MIN_PP_GLOBAL = 4000.0
BEATLEADER_MIN_PP_GLOBAL = 5000.0


# ランキングテーブルのカラム論理インデックスを定数で管理する。
# 見た目の左から右への並び順と一致するように定義しておく。
# 並び: Player | Country | SS(🚩🌍 PP) | BL(🌍🚩 PP) | ACC(🌍🚩 AP) | True(🌍🚩 AP) | Std(🌍🚩 AP) | Tech(🌍🚩 AP) | AvgACC | Plays
COL_PLAYER = 0
COL_COUNTRY = 1

COL_SS_PP = 2
COL_SS_GLOBAL_RANK = 3
COL_SS_COUNTRY_RANK = 4
COL_SS_PLAYS = 5

COL_BL_PP = 6
COL_BL_GLOBAL_RANK = 7
COL_BL_COUNTRY_RANK = 8
COL_BL_PLAYS = 9

COL_AP = 10
COL_ACC_RANK = 11
COL_ACC_COUNTRY_RANK = 12

COL_TRUE_AP = 13
COL_TRUE_ACC_RANK = 14
COL_TRUE_ACC_COUNTRY_RANK = 15

COL_STANDARD_AP = 16
COL_STANDARD_ACC_RANK = 17
COL_STANDARD_ACC_COUNTRY_RANK = 18

COL_TECH_AP = 19
COL_TECH_ACC_RANK = 20
COL_TECH_ACC_COUNTRY_RANK = 21

COL_AVG_ACC = 22
COL_PLAYS = 23
# 合計 24 列 (SS_PLAYS=5, BL_PLAYS=9 は非表示)


class NumericTableWidgetItem(QTableWidgetItem):
    """数値としてソートしたい列用のアイテム。

    表示テキストはそのままに、sort_value に保持した数値で大小比較する。
    """

    def __init__(self, text: str, sort_value: float | int | None = None) -> None:
        super().__init__(text)
        self._sort_value = sort_value

    def __lt__(self, other: "QTableWidgetItem") -> bool:  # type: ignore[override]
        # Numeric の比較は numeric 値を使う
        if isinstance(other, NumericTableWidgetItem):
            a = self._sort_value
            b = other._sort_value

            if a is None and b is None:
                return False
            if a is None:
                # self は空セル → どの値よりも大きい扱い
                return False
            if b is None:
                # other が空セル → self の方が小さい扱い
                return True

            return a < b

        # other が Numeric でない場合は文字列比較のルールに従う
        return TextTableWidgetItem._compare_display(self, other)


class TextTableWidgetItem(QTableWidgetItem):
    """テキスト系セル向け。空文字列を "最も大きい" とみなして昇順時は下に来るようにする。"""

    @staticmethod
    def _compare_display(left_item: "QTableWidgetItem", right_item: "QTableWidgetItem") -> bool:
        left = left_item.data(Qt.ItemDataRole.DisplayRole)
        right = right_item.data(Qt.ItemDataRole.DisplayRole)
        left_str = "" if left is None else str(left)
        right_str = "" if right is None else str(right)

        if left_str == "" and right_str == "":
            return False
        if left_str == "":
            return False
        if right_str == "":
            return True

        return left_str < right_str

    def __lt__(self, other: "QTableWidgetItem") -> bool:  # type: ignore[override]
        return self._compare_display(self, other)


class MainWindow(QMainWindow):
    def __init__(self, initial_steam_id: Optional[str] = None, initial_country_code: Optional[str] = None) -> None:
        super().__init__()
        self.setWindowTitle("My Beat Saber Rank")

        # Stats 画面から渡された「最初にフォーカスしたいプレイヤー」の SteamID
        self._initial_steam_id: Optional[str] = initial_steam_id
        # Stats 画面などから渡された「最初に表示したい国コード」("JP" など, None は Global)
        self._initial_country_code: Optional[str] = initial_country_code.upper() if initial_country_code else None

        central = QWidget(self)
        layout = QVBoxLayout(central)

        # --- フィルタ UI (国選択) ---
        control_row = QHBoxLayout()
        control_row.addWidget(QLabel("Country:"))

        self.country_combo = QComboBox()
        # よく使いそうな国コードをプリセットしつつ、任意入力も許可
        self.country_combo.setEditable(True)
        self.country_combo.addItem("Global (ALL)", userData=None)
        self.country_combo.addItem("Japan (JP)", userData="JP")
        self.country_combo.addItem("United States (US)", userData="US")
        self.country_combo.addItem("Korea (KR)", userData="KR")
        self.country_combo.addItem("China (CN)", userData="CN")
        self.country_combo.addItem("Germany (DE)", userData="DE")
        self.country_combo.addItem("France (FR)", userData="FR")
        self.country_combo.addItem("United Kingdom (GB)", userData="GB")
        self.country_combo.setCurrentIndex(0)  # デフォルト: Global (ALL)

        # 初期国コード指定があれば、コンボボックスの選択を合わせる
        if self._initial_country_code:
            for i in range(self.country_combo.count()):
                data = self.country_combo.itemData(i)
                if isinstance(data, str) and data.upper() == self._initial_country_code:
                    self.country_combo.setCurrentIndex(i)
                    break

        self.country_combo.currentIndexChanged.connect(self.on_country_changed)
        control_row.addWidget(self.country_combo)

        # API ごとに個別にリロードできるボタン
        self.reload_acc_button = QPushButton("Reload AccSaber")
        self.reload_acc_button.clicked.connect(self.reload_accsaber)
        control_row.addWidget(self.reload_acc_button)

        self.reload_ss_button = QPushButton("Reload ScoreSaber")
        self.reload_ss_button.clicked.connect(self.reload_scoresaber)
        control_row.addWidget(self.reload_ss_button)

        self.reload_bl_button = QPushButton("Reload BeatLeader")
        self.reload_bl_button.clicked.connect(self.reload_beatleader)
        control_row.addWidget(self.reload_bl_button)

        # 一度だけ深く同期してプレイヤーインデックスを構築するためのボタン
        self.full_sync_button = QPushButton("Full Sync (Index)")
        self.full_sync_button.clicked.connect(self.full_sync)
        control_row.addWidget(self.full_sync_button)

        control_row.addStretch(1)
        layout.addLayout(control_row)

        # --- ランキングテーブル ---
        # Country カラムを追加して、どの国のランキングかを明示する
        # AccSaber の Country Rank / True / Standard / Tech AP と BeatLeader の PP / ランクも併せて表示するため 16+α カラム構成
        self.table = QTableWidget(0, 24, self)
        self.table.verticalHeader().setDefaultSectionSize(9)  # 行の高さを少し詰める
        self.table.verticalHeader().setStretchLastSection(True)

        header = self.table.horizontalHeader()
        header.setDefaultSectionSize(80)  # 列の幅を少し広げる
        header.setStretchLastSection(True)  # 最後の列を伸縮可能にする
        # ユーザーがカラム順をドラッグで変更できるようにする
        header.setSectionsMovable(True)
        # 行をストライプにする
        self.table.setAlternatingRowColors(True)
        self.table.setStyleSheet("QTableWidget { background-color: #ffffff; alternate-background-color: #f6f7fb; }")
        
        font = self.table.font()
        font.setPointSizeF(9.0)  # フォントサイズを少し小さくする
        self.table.setFont(font)

        # カラム名を「見た目の左から右の順」に定義しておく。
        # ※ 論理インデックスもこの順と一致する。
        headers = [""] * self.table.columnCount()
        headers[COL_PLAYER] = "Player"
        headers[COL_COUNTRY] = "🚩"

        headers[COL_SS_COUNTRY_RANK] = "🚩"             # ScoreSaber Country Rank
        headers[COL_SS_GLOBAL_RANK] = "🌍"              # ScoreSaber Global Rank
        headers[COL_SS_PP] = "PP"                       # ScoreSaber PP
        headers[COL_SS_PLAYS] = ""                      # hidden

        headers[COL_BL_GLOBAL_RANK] = "🌍"              # BeatLeader Global Rank
        headers[COL_BL_COUNTRY_RANK] = "🚩"             # BeatLeader Country Rank
        headers[COL_BL_PP] = "PP"                       # BeatLeader PP
        headers[COL_BL_PLAYS] = ""                      # hidden

        headers[COL_ACC_RANK] = "🌍"                    # AccSaber Overall Rank
        headers[COL_ACC_COUNTRY_RANK] = "🚩"            # AccSaber Overall Country Rank
        headers[COL_AP] = "AP"                          # AccSaber Overall AP

        headers[COL_TRUE_ACC_RANK] = "True🌍"            # True Acc Rank
        headers[COL_TRUE_ACC_COUNTRY_RANK] = "True🚩"    # True Acc Country Rank
        headers[COL_TRUE_AP] = "True AP"

        headers[COL_STANDARD_ACC_RANK] = "Std🌍"         # Standard Acc Rank
        headers[COL_STANDARD_ACC_COUNTRY_RANK] = "Std🚩" # Standard Acc Country Rank
        headers[COL_STANDARD_AP] = "Std AP"

        headers[COL_TECH_ACC_RANK] = "Tech🌍"            # Tech Acc Rank
        headers[COL_TECH_ACC_COUNTRY_RANK] = "Tech🚩"    # Tech Acc Country Rank
        headers[COL_TECH_AP] = "Tech AP"

        headers[COL_AVG_ACC] = "Avg ACC"
        headers[COL_PLAYS] = "Plays"
        self.table.setHorizontalHeaderLabels(headers)

        # サービス名 (ACC/SS/BL) の区別はヘッダーアイコンで行う。
        self._apply_header_icons()

        # 代表的なカラムの初期幅を設定
        self.table.setColumnWidth(COL_PLAYER, 220)     # Player 列は名前が見やすいように広めに
        self.table.setColumnWidth(COL_COUNTRY, 40)

        self.table.setColumnWidth(COL_SS_COUNTRY_RANK, 40)
        self.table.setColumnWidth(COL_SS_GLOBAL_RANK, 60)
        self.table.setColumnWidth(COL_SS_PP, 80)

        self.table.setColumnWidth(COL_BL_GLOBAL_RANK, 60)
        self.table.setColumnWidth(COL_BL_COUNTRY_RANK, 40)
        self.table.setColumnWidth(COL_BL_PP, 80)

        self.table.setColumnWidth(COL_ACC_RANK, 45)
        self.table.setColumnWidth(COL_ACC_COUNTRY_RANK, 45)

        self.table.setColumnWidth(COL_TRUE_ACC_RANK, 65)
        self.table.setColumnWidth(COL_TRUE_ACC_COUNTRY_RANK, 65)
        self.table.setColumnWidth(COL_STANDARD_ACC_RANK, 65)
        self.table.setColumnWidth(COL_STANDARD_ACC_COUNTRY_RANK, 65)
        self.table.setColumnWidth(COL_TECH_ACC_RANK, 65)
        self.table.setColumnWidth(COL_TECH_ACC_COUNTRY_RANK, 65)

        # SS/BL の Plays 列は非表示
        self.table.setColumnHidden(COL_SS_PLAYS, True)
        self.table.setColumnHidden(COL_BL_PLAYS, True)



        # ヘッダークリックで各列をソートできるようにする
        self.table.setSortingEnabled(True)

        layout.addWidget(self.table)
        self.setCentralWidget(central)

        # データ保持用のフィールド（起動時はキャッシュから読み込む）
        self.acc_players: list[AccSaberPlayer] = []
        self.ss_players: list[ScoreSaberPlayer] = []
        self.bl_players: Dict[str, BeatLeaderPlayer] = {}

        # SteamID(17桁) をキーに、ScoreSaber / BeatLeader 情報をまとめたインデックス
        # { steam_id: {"scoresaber": ScoreSaberPlayer, "beatleader": BeatLeaderPlayer} }
        self.player_index: Dict[str, Dict[str, object]] = {}

        self._load_all_caches_for_current_country()
        self._load_player_index()
        country = self._current_country_code()
        self._populate_table(self.acc_players, self.ss_players, country)

        # 初期フォーカス指定があれば、一度だけ該当プレイヤー行へスクロールする
        if self._initial_steam_id:
            self.focus_on_steam_id(self._initial_steam_id)

        # 起動直後にキャッシュが無くテーブルが空の場合は、
        # 一度だけ自動的にリロードしてランキングを表示する。
        if self.table.rowCount() == 0:
            self.reload_accsaber(update_table=False)
            self.reload_scoresaber(update_table=False)
            # BeatLeader は ScoreSaber ID を元にするため、後から手動リロードでも十分だが
            # 起動時に自動取得しておくと分かりやすいので試みる。
            self.reload_beatleader(update_table=False)

            country = self._current_country_code()
            self._populate_table(self.acc_players, self.ss_players, country)

            if self._initial_steam_id:
                self.focus_on_steam_id(self._initial_steam_id)

    def _apply_header_icons(self) -> None:
        """各カラムに対応するサービスのアイコンを設定する。"""

        # Resolve icon files via helper that handles frozen/packaged/development layouts
        ss_icon_path = resource_path("scoresaber_logo.svg")
        bl_icon_path = resource_path("beatleader_logo.jpg")
        acc_icon_path = resource_path("asssaber_logo.webp")

        ss_icon = QIcon(str(ss_icon_path)) if ss_icon_path.exists() else QIcon()
        bl_icon = QIcon(str(bl_icon_path)) if bl_icon_path.exists() else QIcon()
        acc_icon = QIcon(str(acc_icon_path)) if acc_icon_path.exists() else QIcon()

        # 論理インデックス定数に基づいてサービス別のカラムを定義
        acc_cols = [
            # AccSaber 全列にアイコンを付与する
            COL_ACC_RANK,
            COL_ACC_COUNTRY_RANK,
            COL_AP,
            COL_TRUE_ACC_RANK,
            COL_TRUE_ACC_COUNTRY_RANK,
            COL_TRUE_AP,
            COL_STANDARD_ACC_RANK,
            COL_STANDARD_ACC_COUNTRY_RANK,
            COL_STANDARD_AP,
            COL_TECH_ACC_RANK,
            COL_TECH_ACC_COUNTRY_RANK,
            COL_TECH_AP,
            COL_AVG_ACC,
            COL_PLAYS,
        ]
        ss_cols = [
            COL_SS_GLOBAL_RANK,
            COL_SS_COUNTRY_RANK,
            COL_SS_PP,
        ]
        bl_cols = [
            COL_BL_GLOBAL_RANK,
            COL_BL_COUNTRY_RANK,
            COL_BL_PP,
        ]

        for col in range(self.table.columnCount()):
            item = self.table.horizontalHeaderItem(col)
            if item is None:
                continue

            # テキストは setHorizontalHeaderLabels で定義したものをそのまま使い、
            # ここではアイコンだけを付与する。
            if col in acc_cols:
                item.setIcon(acc_icon)
            elif col in ss_cols:
                item.setIcon(ss_icon)
            elif col in bl_cols:
                item.setIcon(bl_icon)

            self.table.setHorizontalHeaderItem(col, item)

    def _create_progress_dialog(self, title: str, label_text: str, maximum: int) -> QProgressDialog:
        dialog = QProgressDialog(label_text, "Cancel", 0, maximum, self)
        dialog.setWindowTitle(title)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setAutoClose(True)
        dialog.setAutoReset(True)
        dialog.show()
        QApplication.processEvents()
        return dialog

    def _current_country_code(self) -> Optional[str]:
        # コンボの userData を優先し、なければテキストから推測
        user_data = self.country_combo.currentData()
        if isinstance(user_data, str) and user_data:
            return user_data.upper()

        text = self.country_combo.currentText().strip()
        if len(text) == 2:
            return text.upper()
        return None

    # --- キャッシュ関連 ---

    def _cache_prefix(self) -> str:
        country = self._current_country_code()
        return (country or "ALL").upper()

    def _acc_cache_path(self) -> Path:
        """AccSaber は国別情報を持たないので、常に単一ファイルを使う。"""

        return CACHE_DIR / "accsaber_ranking.json"

    def _ss_cache_path(self) -> Path:
        return CACHE_DIR / f"scoresaber_{self._cache_prefix()}.json"

    def _bl_cache_path(self) -> Path:
        return CACHE_DIR / f"beatleader_{self._cache_prefix()}.json"

    def _player_index_path(self) -> Path:
        return CACHE_DIR / "players_index.json"

    def _load_list_cache(self, path: Path, cls):
        if not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return [cls(**item) for item in data]
        except Exception:  # noqa: BLE001
            return []

    def _save_list_cache(self, path: Path, items) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            serializable = [asdict(x) for x in items]
            with path.open("w", encoding="utf-8") as f:
                json.dump(serializable, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            # キャッシュ保存失敗時は黙って無視（表示は継続する）
            return

    def _load_bl_cache(self, path: Path) -> Dict[str, BeatLeaderPlayer]:
        if not path.exists():
            return {}
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            players = [BeatLeaderPlayer(**item) for item in data]
            return {p.id: p for p in players if p.id}
        except Exception:  # noqa: BLE001
            return {}

    def _save_bl_cache(self, path: Path, mapping: Dict[str, BeatLeaderPlayer]) -> None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            serializable = [asdict(p) for p in mapping.values()]
            with path.open("w", encoding="utf-8") as f:
                json.dump(serializable, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            return

    def _load_player_index(self) -> None:
        """
        プレイヤーインデックスを JSON ファイルから読み込む。
        
        :param self: 説明
        """
        
        path = self._player_index_path()
        if not path.exists():
            self.player_index = {}
            return

        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:  # noqa: BLE001
            self.player_index = {}
            return

        index: Dict[str, Dict[str, object]] = {}
        for row in data:
            steam_id = str(row.get("steam_id", ""))
            if not steam_id:
                continue

            entry: Dict[str, object] = {}
            ss_data = row.get("scoresaber")
            if isinstance(ss_data, dict):
                try:
                    entry["scoresaber"] = ScoreSaberPlayer(**ss_data)
                except TypeError:
                    pass

            bl_data = row.get("beatleader")
            if isinstance(bl_data, dict):
                try:
                    entry["beatleader"] = BeatLeaderPlayer(**bl_data)
                except TypeError:
                    pass

            if entry:
                index[steam_id] = entry

        self.player_index = index

    def _save_player_index(self) -> None:
        """
        プレイヤーインデックスを JSON ファイルに保存する。
        :param self: 
        """
        path = self._player_index_path()
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            rows = []
            for steam_id, entry in self.player_index.items():
                row: dict[str, object] = {"steam_id": steam_id}
                ss = entry.get("scoresaber")
                bl = entry.get("beatleader")
                if isinstance(ss, ScoreSaberPlayer):
                    row["scoresaber"] = asdict(ss)
                if isinstance(bl, BeatLeaderPlayer):
                    row["beatleader"] = asdict(bl)
                rows.append(row)

            with path.open("w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            return

    def _load_all_caches_for_current_country(self) -> None:
        # AccSaber は国別ではないので、固定ファイルを読む。
        # 互換性のため、旧ファイル名があればそれもフォールバックで読む。
        acc_path = self._acc_cache_path()
        if acc_path.exists():
            self.acc_players = self._load_list_cache(acc_path, AccSaberPlayer)
        else:
            # 旧バージョンのファイル名へのフォールバック
            legacy_paths = [
                CACHE_DIR / "accsaber_ALL.json",
                CACHE_DIR / "accsaber_JP.json",
            ]
            for lp in legacy_paths:
                if lp.exists():
                    self.acc_players = self._load_list_cache(lp, AccSaberPlayer)
                    break
            else:
                self.acc_players = []

        # ScoreSaber: 国別キャッシュがなければグローバル(ranking)をフォールバックで使う
        ss_path = self._ss_cache_path()
        if ss_path.exists():
            self.ss_players = self._load_list_cache(ss_path, ScoreSaberPlayer)
        else:
            all_path = CACHE_DIR / "scoresaber_ranking.json"
            if all_path.exists():
                self.ss_players = self._load_list_cache(all_path, ScoreSaberPlayer)
            else:
                self.ss_players = []

        # BeatLeader: 現在の国用キャッシュがあればそれを優先し、
        # 無ければ/空であれば beatleader_ranking.json から現在の国コードに
        # 合致するプレイヤーをすべて読み込む。
        bl_mapping = self._load_bl_cache(self._bl_cache_path())

        if not bl_mapping:
            global_path = CACHE_DIR / "beatleader_ranking.json"
            if global_path.exists():
                try:
                    with global_path.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    players = [BeatLeaderPlayer(**item) for item in data if isinstance(item, dict)]

                    current_country = self._current_country_code()
                    if current_country:
                        cc = current_country.upper()
                        players = [p for p in players if (p.country or "").upper() == cc]

                    bl_mapping = {p.id: p for p in players if p.id}
                except Exception:  # noqa: BLE001
                    bl_mapping = {}

        self.bl_players = bl_mapping

    def on_country_changed(self, index: int) -> None:  # noqa: ARG002
        """国選択が変わったときは、その国向けのキャッシュを読み直して即座に表示だけ更新する。"""

        self._load_all_caches_for_current_country()
        country = self._current_country_code()
        self._populate_table(self.acc_players, self.ss_players, country)

        # 国を切り替えた後も、可能なら同じ SteamID の行を探してフォーカスする
        if self._initial_steam_id:
            self.focus_on_steam_id(self._initial_steam_id)

    def reload_leaderboard(self) -> None:
        """互換用: 現在の国コードで AccSaber / ScoreSaber / BeatLeader をまとめて再取得する。"""

        self.reload_accsaber(update_table=False)
        self.reload_scoresaber(update_table=False)
        self.reload_beatleader(update_table=False)

        country = self._current_country_code()
        self._populate_table(self.acc_players, self.ss_players, country)

    # --- API リロード ---

    def reload_accsaber(self, update_table: bool = True) -> None:
        """AccSaber のオーバーオールランキングを API から再取得し、キャッシュを更新する。"""

        # AccSaber は国別情報を持たない前提で、常に Global オーバーオールを取得する
        try:
            acc_players: list[AccSaberPlayer] = []
            # 安全上限ページ数。AP がしきい値未満になったところで打ち切る想定。
            max_pages = 200
            progress = self._create_progress_dialog("AccSaber", "Loading AccSaber overall leaderboard...", max_pages)

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

            for page in range(1, max_pages + 1):
                if progress.wasCanceled():
                    break
                progress.setValue(page - 1)
                QApplication.processEvents()
                page_players = fetch_overall(country=None, page=page)
                if not page_players:
                    break
                # total_ap から AP をパースし、しきい値以上だけを採用する
                for p in page_players:
                    ap_value = _parse_ap(getattr(p, "total_ap", ""))
                    if ap_value >= ACCSABER_MIN_AP_GLOBAL:
                        acc_players.append(p)
                # 最後のプレイヤーの AP がしきい値を下回ったら、それ以降のページも対象外とみなして打ち切る
                last_ap = _parse_ap(getattr(page_players[-1], "total_ap", ""))
                if last_ap < ACCSABER_MIN_AP_GLOBAL:
                    break
            progress.setValue(max_pages)
            progress.close()

            # 取得した Overall ランキングのプレイヤーを ID で引けるようにしておく
            by_id: dict[str, AccSaberPlayer] = {}
            for p in acc_players:
                if getattr(p, "scoresaber_id", None):
                    by_id[str(p.scoresaber_id)] = p

            # True / Standard / Tech 各リーダーボードから AP を取得し、
            # scoresaber_id をキーに Overall 側のプレイヤーへ埋め込む。
            # 表示しているプレイヤー全員をできるだけ埋めるため、
            # 対象IDをすべて解決するか、ページが尽きるまでページングする。
            def _enrich_skill(leaderboard_fetch, attr_name: str) -> None:
                max_pages_skill = 200  # 安全上限。データが尽きたら途中で抜ける。
                for page in range(1, max_pages_skill + 1):
                    skill_players = leaderboard_fetch(country=None, page=page)
                    if not skill_players:
                        break

                    for sp in skill_players:
                        sid = getattr(sp, "scoresaber_id", None)
                        if not sid:
                            continue
                        sid_str = str(sid)
                        if sid_str in by_id:
                            # Overall にも存在するプレイヤー → スキル AP を埋め込む
                            setattr(by_id[sid_str], attr_name, sp.total_ap)
                        else:
                            # Overall に存在しないカテゴリ専用プレイヤー → 新規エントリとして追加
                            # Country Rank の母集団に含めるために必要
                            new_p = AccSaberPlayer(
                                rank=getattr(sp, "rank", 0),
                                name=getattr(sp, "name", ""),
                                total_ap="0",
                                average_acc=getattr(sp, "average_acc", ""),
                                plays=getattr(sp, "plays", ""),
                                top_play_pp=getattr(sp, "top_play_pp", ""),
                                scoresaber_id=sid_str,
                            )
                            setattr(new_p, attr_name, sp.total_ap)
                            acc_players.append(new_p)
                            by_id[sid_str] = new_p

            try:
                _enrich_skill(fetch_true, "true_ap")
                _enrich_skill(fetch_standard, "standard_ap")
                _enrich_skill(fetch_tech, "tech_ap")
            except Exception:
                # AP 詳細取得に失敗しても Overall 自体は使えるようにする
                pass
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Error", f"Failed to load AccSaber leaderboard:\n{exc}")
            return

        self.acc_players = acc_players
        self._save_list_cache(self._acc_cache_path(), self.acc_players)

        if update_table:
            country = self._current_country_code()
            self._populate_table(self.acc_players, self.ss_players, country)

    def reload_scoresaber(self, update_table: bool = True) -> None:
        """現在の国コードで ScoreSaber のランキングだけを API から再取得し、キャッシュを更新する。"""

        country = self._current_country_code()

        progress: Optional[QProgressDialog] = None
        try:
            if country:
                ss_players: list[ScoreSaberPlayer] = []
                max_pages_ss = 120  # 50件/ページとして最大6000件程度
                progress = self._create_progress_dialog(
                    "ScoreSaber",
                    f"Loading ScoreSaber rankings ({country.upper()})...",
                    max_pages_ss,
                )
                for page in range(1, max_pages_ss + 1):
                    if progress is not None and progress.wasCanceled():
                        break
                    progress.setValue(page - 1)
                    QApplication.processEvents()
                    page_players = fetch_players(country=country, page=page)
                    if not page_players:
                        break
                    ss_players.extend(page_players)
            else:
                progress = self._create_progress_dialog("ScoreSaber", "Loading ScoreSaber global rankings...", 1)
                ss_players = fetch_players(country=None, page=1)
                progress.setValue(1)
                QApplication.processEvents()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Warning", f"Failed to load ScoreSaber data:\n{exc}")
            ss_players = []
        finally:
            if progress is not None:
                progress.close()

        self.ss_players = ss_players
        self._save_list_cache(self._ss_cache_path(), self.ss_players)

        if update_table:
            self._populate_table(self.acc_players, self.ss_players, country)

    def _is_steam_id(self, value: str) -> bool:
        return value.isdigit() and len(value) == 17

    def _rebuild_player_index_from_global(self) -> None:
        """グローバルキャッシュ(scoresaber_ALL / beatleader_ALL)からプレイヤーインデックスを構築する。"""

        # collector 側の共通実装で players_index.json を再構築し、
        # その内容を再読み込みする。
        rebuild_player_index_from_global()
        self._load_player_index()

    def reload_beatleader(self, update_table: bool = True) -> None:
        """BeatLeader ランキングを一括取得してキャッシュを更新する。

        個別プレイヤー fetch ではなく /players エンドポイントのページング取得を使うため高速。
        """
        country = self._current_country_code()

        max_pages_bl = 200
        progress = self._create_progress_dialog(
            "BeatLeader",
            "Loading BeatLeader rankings...",
            max_pages_bl,
        )

        def _on_progress(page: int, total_pages: int) -> None:
            if progress.wasCanceled():
                raise RuntimeError("BL_RELOAD_CANCELLED")
            progress.setMaximum(max(max_pages_bl, total_pages))
            progress.setValue(min(page, progress.maximum()))
            QApplication.processEvents()

        try:
            all_bl_players = fetch_players_ranking(
                min_pp=BEATLEADER_MIN_PP_GLOBAL if country is None else 0.0,
                country=country,
                progress=_on_progress,
                max_pages=max_pages_bl,
            )
        except RuntimeError as exc:
            progress.close()
            if "BL_RELOAD_CANCELLED" not in str(exc):
                QMessageBox.critical(self, "Error", f"Failed to load BeatLeader leaderboard:\n{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            progress.close()
            QMessageBox.critical(self, "Error", f"Failed to load BeatLeader leaderboard:\n{exc}")
            return

        progress.close()

        self.bl_players = {p.id: p for p in all_bl_players if p.id}
        self._save_bl_cache(self._bl_cache_path(), self.bl_players)

        if update_table:
            self._populate_table(self.acc_players, self.ss_players, country)

    def full_sync(self) -> None:
        """AccSaber / ScoreSaber / BeatLeader のランキングと players_index をまとめて更新する。"""

        progress = self._create_progress_dialog("Fetch Ranking Data", "Fetching ranking data...", 100)
        progress.setWindowTitle("Fetch Ranking Data")
        progress.setAutoClose(True)
        progress.setAutoReset(True)
        progress.show()

        def _on_progress(message: str, fraction: float) -> None:
            if progress.wasCanceled():
                raise RuntimeError("FULL_SYNC_CANCELLED")
            value = int(max(0.0, min(1.0, fraction)) * 100)
            progress.setValue(value)
            progress.setLabelText(message)
            QApplication.processEvents()

        try:
            ensure_global_rank_caches(progress=_on_progress)
        except RuntimeError as exc:
            if "FULL_SYNC_CANCELLED" in str(exc):
                # ユーザーキャンセル時はそのまま終了
                pass
            else:
                QMessageBox.warning(self, "Full Sync", f"Failed to fetch ranking data:\n{exc}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Full Sync", f"Failed to fetch ranking data:\n{exc}")
        finally:
            progress.close()

        # 最新キャッシュを読み直してテーブルを更新
        self._load_all_caches_for_current_country()
        country = self._current_country_code()
        self._populate_table(self.acc_players, self.ss_players, country)
        self.statusBar().clearMessage()

        # 現在の国設定でテーブルを再描画（AccSaber は最新キャッシュが使われる）
        country = self._current_country_code()
        self._populate_table(self.acc_players, self.ss_players, country)
        self.statusBar().clearMessage()
        QMessageBox.information(self, "Full Sync", "Player index has been rebuilt.")

    def _populate_table(
        self,
        acc_players: list[AccSaberPlayer],
        ss_players: list[ScoreSaberPlayer],
        country: Optional[str],
    ) -> None:
        # テーブル描画中にソートが走ると、行インデックスとプレイヤーがずれて
        # 「ACC Rank だけ埋まって他の列が空」のような状態になるため、
        # 一時的にソートを無効化してから行を追加する。
        was_sorting_enabled = self.table.isSortingEnabled()
        if was_sorting_enabled:
            self.table.setSortingEnabled(False)
        def _parse_float(text: str) -> float:
            if not text:
                return 0.0
            t = text.replace(",", "")
            m = re.search(r"[-+]?\d*\.?\d+", t)
            if not m:
                return 0.0
            try:
                return float(m.group(0))
            except ValueError:
                return 0.0

        def _parse_int(text: str) -> int:
            if not text:
                return 0
            t = text.replace(",", "")
            m = re.search(r"[-+]?\d+", t)
            if not m:
                return 0
            try:
                return int(m.group(0))
            except ValueError:
                return 0

        # ScoreSaber 側を ID / 名前で引けるようにインデックス化
        ss_index_by_id: dict[str, ScoreSaberPlayer] = {}
        ss_index_by_name: dict[str, ScoreSaberPlayer] = {}
        for p in ss_players:
            if p.id:
                ss_index_by_id[p.id] = p
            if p.name:
                # 大文字小文字は無視して名前マップも作る
                ss_index_by_name[p.name.lower()] = p

        # players_index.json を使って、ScoreSaber ID ごとの国コードを集約する
        # players_index に無いプレイヤーは ss_players / bl_players から補完する。
        ss_country_by_id: dict[str, str] = {}
        for entry in self.player_index.values():
            ss_pi = entry.get("scoresaber")
            if isinstance(ss_pi, ScoreSaberPlayer) and ss_pi.id and ss_pi.country:
                ss_country_by_id[ss_pi.id] = ss_pi.country.upper()

        # players_index に無い ScoreSaber プレイヤーをランキングデータで補完
        # （BL-only として登録されているが実際は SS にも存在するプレイヤー対応）
        for ss_p in ss_players:
            if ss_p.id and ss_p.country and ss_p.id not in ss_country_by_id:
                ss_country_by_id[ss_p.id] = ss_p.country.upper()

        # BeatLeader プレイヤーの国コードも補完（players_index に無い BL 専用プレイヤー対応）
        for bl_p in self.bl_players.values():
            if bl_p.id and bl_p.country and bl_p.id not in ss_country_by_id:
                ss_country_by_id[bl_p.id] = bl_p.country.upper()

        # グローバル SS キャッシュからも補完する。
        # country フィルタ表示時は ss_players が指定国のみになるため、
        # AccSaber の Country Rank 計算の母集団（同一国の全 AccSaber プレイヤー）が
        # 正確になるようにグローバルキャッシュも参照する。
        # player_app.py の _load_player_index_countries と同じ方針。
        for _ss_global_name in ["scoresaber_ranking.json", "scoresaber_ALL.json"]:
            _ss_global_path = CACHE_DIR / _ss_global_name
            if not _ss_global_path.exists():
                continue
            try:
                _ss_global_data = json.loads(_ss_global_path.read_text(encoding="utf-8"))
                for _item in _ss_global_data:
                    if not isinstance(_item, dict):
                        continue
                    _sid = str(_item.get("id") or "")
                    _cc = str(_item.get("country") or "").upper()
                    if _sid and _cc and _sid not in ss_country_by_id:
                        ss_country_by_id[_sid] = _cc
            except Exception:  # noqa: BLE001
                continue

        # AccSaber 側の各種 Rank / Country Rank を事前計算しておく
        acc_country_rank: dict[str, int] = {}
        true_rank_by_sid: dict[str, int] = {}
        standard_rank_by_sid: dict[str, int] = {}
        tech_rank_by_sid: dict[str, int] = {}

        true_country_rank_by_sid: dict[str, int] = {}
        standard_country_rank_by_sid: dict[str, int] = {}
        tech_country_rank_by_sid: dict[str, int] = {}

        players_by_country: dict[str, list[AccSaberPlayer]] = {}
        players_by_country_true: dict[str, list[AccSaberPlayer]] = {}
        players_by_country_standard: dict[str, list[AccSaberPlayer]] = {}
        players_by_country_tech: dict[str, list[AccSaberPlayer]] = {}

        for acc in acc_players:
            sid = getattr(acc, "scoresaber_id", None)
            if not sid:
                continue
            sid_str = str(sid)
            cc = ss_country_by_id.get(sid_str)
            if not cc:
                continue
            players_by_country.setdefault(cc, []).append(acc)

            # スキル別 Country Rank 用に、AP があるプレイヤーだけを集約
            if _parse_float(getattr(acc, "true_ap", "")) > 0.0:
                players_by_country_true.setdefault(cc, []).append(acc)
            if _parse_float(getattr(acc, "standard_ap", "")) > 0.0:
                players_by_country_standard.setdefault(cc, []).append(acc)
            if _parse_float(getattr(acc, "tech_ap", "")) > 0.0:
                players_by_country_tech.setdefault(cc, []).append(acc)

        # Overall Country Rank
        for cc, plist in players_by_country.items():  # noqa: B007
            plist_sorted = sorted(
                plist,
                key=lambda p: _parse_float(getattr(p, "total_ap", "")),
                reverse=True,
            )
            rank_val = 1
            for acc in plist_sorted:
                sid = getattr(acc, "scoresaber_id", None)
                if not sid:
                    continue
                sid_str = str(sid)
                if sid_str in acc_country_rank:
                    continue
                acc_country_rank[sid_str] = rank_val
                rank_val += 1

        # True / Standard / Tech の Global Rank
        def _build_skill_global_ranks(attr_name: str, target: dict[str, int]) -> None:
            plist = [a for a in acc_players if _parse_float(getattr(a, attr_name, "")) > 0.0]
            plist_sorted = sorted(
                plist,
                key=lambda p: _parse_float(getattr(p, attr_name, "")),
                reverse=True,
            )
            rank_val = 1
            for acc in plist_sorted:
                sid = getattr(acc, "scoresaber_id", None)
                if not sid:
                    continue
                sid_str = str(sid)
                if sid_str in target:
                    continue
                target[sid_str] = rank_val
                rank_val += 1

        _build_skill_global_ranks("true_ap", true_rank_by_sid)
        _build_skill_global_ranks("standard_ap", standard_rank_by_sid)
        _build_skill_global_ranks("tech_ap", tech_rank_by_sid)

        # True / Standard / Tech の Country Rank
        def _build_skill_country_ranks(
            players_by_cc: dict[str, list[AccSaberPlayer]],
            attr_name: str,
            target: dict[str, int],
        ) -> None:
            for cc, plist in players_by_cc.items():  # noqa: B007
                plist_sorted = sorted(
                    plist,
                    key=lambda p: _parse_float(getattr(p, attr_name, "")),
                    reverse=True,
                )
                rank_val = 1
                for acc in plist_sorted:
                    sid = getattr(acc, "scoresaber_id", None)
                    if not sid:
                        continue
                    sid_str = str(sid)
                    if sid_str in target:
                        continue
                    target[sid_str] = rank_val
                    rank_val += 1

        _build_skill_country_ranks(players_by_country_true, "true_ap", true_country_rank_by_sid)
        _build_skill_country_ranks(players_by_country_standard, "standard_ap", standard_country_rank_by_sid)
        _build_skill_country_ranks(players_by_country_tech, "tech_ap", tech_country_rank_by_sid)

        # ScoreSaber ID -> AccSaber プレイヤー のマップ
        acc_by_sid: dict[str, AccSaberPlayer] = {}
        for acc in acc_players:
            sid = getattr(acc, "scoresaber_id", None)
            if not sid:
                continue
            acc_by_sid[str(sid)] = acc

        # いったんテーブルをクリアしてから、条件に合う行だけ追加する
        self.table.setRowCount(0)

        # いったんテーブルをクリアしてから、条件に合う行だけ追加する
        self.table.setRowCount(0)

        # まずは ScoreSaber ランキングをベースに行を作成し、そこに AccSaber / BeatLeader 情報を紐付ける。
        # どの BeatLeader プレイヤーが「ScoreSaber 行に紐づいたか」を記録しておき、
        # 後続の BeatLeader 専用行追加時に重複しないようにする。
        attached_bl_ids: set[str] = set()

        for ss in ss_players:
            if not ss.id:
                continue

            sid = ss.id
            bl: Optional[BeatLeaderPlayer] = None

            # 1. フル同期で構築したプレイヤーインデックスから BeatLeader を参照
            if self.player_index:
                entry = self.player_index.get(sid)
                if entry:
                    bl_obj = entry.get("beatleader")
                    if isinstance(bl_obj, BeatLeaderPlayer):
                        bl = bl_obj

            # 2. インデックスで見つからなかった場合は、BeatLeader キャッシュから参照
            if bl is None:
                bl = self.bl_players.get(sid)

            # AccSaber 側は scoresaber_id から引く
            acc: Optional[AccSaberPlayer] = acc_by_sid.get(sid)

            ss_ok = False
            bl_ok = False

            # 国コードが指定されている場合は、ScoreSaber / BeatLeader の
            # いずれかでその国と判定できるプレイヤーだけを表示する。
            if country is not None:
                target = country.upper()
                ss_ok = bool(ss.country and ss.country.upper() == target)
                bl_ok = bool(bl and (bl.country or "").upper() == target)
                if not (ss_ok or bl_ok):
                    continue

            row = self.table.rowCount()
            self.table.insertRow(row)

            # ACC Rank / True/Standard/Tech Rank / Country Rank (数値ソート)
            if acc is not None:
                # Overall Rank
                self.table.setItem(row, COL_ACC_RANK, NumericTableWidgetItem(str(acc.rank), acc.rank))

                # True / Standard / Tech Global Rank
                t_rank = true_rank_by_sid.get(sid)
                t_rank_text = "" if t_rank is None else str(t_rank)
                self.table.setItem(row, COL_TRUE_ACC_RANK, NumericTableWidgetItem(t_rank_text, t_rank))

                s_rank = standard_rank_by_sid.get(sid)
                s_rank_text = "" if s_rank is None else str(s_rank)
                self.table.setItem(row, COL_STANDARD_ACC_RANK, NumericTableWidgetItem(s_rank_text, s_rank))

                te_rank = tech_rank_by_sid.get(sid)
                te_rank_text = "" if te_rank is None else str(te_rank)
                self.table.setItem(row, COL_TECH_ACC_RANK, NumericTableWidgetItem(te_rank_text, te_rank))

                # Country Rank 群
                acc_country_rank_val: Optional[int] = acc_country_rank.get(sid)
                acc_cr_text = "" if acc_country_rank_val is None else str(acc_country_rank_val)
                self.table.setItem(
                    row,
                    COL_ACC_COUNTRY_RANK,
                    NumericTableWidgetItem(acc_cr_text, acc_country_rank_val),
                )

                t_cr = true_country_rank_by_sid.get(sid)
                t_cr_text = "" if t_cr is None else str(t_cr)
                self.table.setItem(row, COL_TRUE_ACC_COUNTRY_RANK, NumericTableWidgetItem(t_cr_text, t_cr))

                s_cr = standard_country_rank_by_sid.get(sid)
                s_cr_text = "" if s_cr is None else str(s_cr)
                self.table.setItem(row, COL_STANDARD_ACC_COUNTRY_RANK, NumericTableWidgetItem(s_cr_text, s_cr))

                te_cr = tech_country_rank_by_sid.get(sid)
                te_cr_text = "" if te_cr is None else str(te_cr)
                self.table.setItem(row, COL_TECH_ACC_COUNTRY_RANK, NumericTableWidgetItem(te_cr_text, te_cr))
            else:
                for col in [
                    COL_ACC_RANK,
                    COL_TRUE_ACC_RANK,
                    COL_STANDARD_ACC_RANK,
                    COL_TECH_ACC_RANK,
                    COL_ACC_COUNTRY_RANK,
                    COL_TRUE_ACC_COUNTRY_RANK,
                    COL_STANDARD_ACC_COUNTRY_RANK,
                    COL_TECH_ACC_COUNTRY_RANK,
                ]:
                    self.table.setItem(row, col, NumericTableWidgetItem("", None))

            # Player / Country 列
            # Player セルには、その行に対応する scoresaber_id(=SteamID) を UserRole に保持しておく
            player_item = TextTableWidgetItem(ss.name)
            player_item.setData(Qt.ItemDataRole.UserRole, sid)
            self.table.setItem(row, COL_PLAYER, player_item)

            if country is not None:
                # フィルタ中は、まず ScoreSaber の国コードを優先し、
                # 無い場合のみ BeatLeader 側の国コードを使う。
                if ss is not None and ss.country:
                    country_text = ss.country.upper()
                elif bl is not None and bl.country:
                    country_text = bl.country.upper()
                else:
                    country_text = country.upper()
            elif ss is not None:
                # Global(ALL) のときは ScoreSaber の国コードを表示
                country_text = ss.country.upper()
            elif bl is not None and bl.country:
                # ScoreSaber 情報が無くても BeatLeader 側に国コードがあればそれを表示
                country_text = bl.country.upper()
            else:
                country_text = ""

            self.table.setItem(row, COL_COUNTRY, TextTableWidgetItem(country_text))

            # AP 系列・平均ACC・Plays は数値ソート (AccSaber 未参加の場合は空欄)
            if acc is not None:
                total_ap_val = _parse_float(acc.total_ap)
                self.table.setItem(row, COL_AP, NumericTableWidgetItem(acc.total_ap, total_ap_val))

                true_ap_text = getattr(acc, "true_ap", "")
                true_ap_val = _parse_float(true_ap_text)
                self.table.setItem(row, COL_TRUE_AP, NumericTableWidgetItem(true_ap_text, true_ap_val))

                standard_ap_text = getattr(acc, "standard_ap", "")
                standard_ap_val = _parse_float(standard_ap_text)
                self.table.setItem(
                    row,
                    COL_STANDARD_AP,
                    NumericTableWidgetItem(standard_ap_text, standard_ap_val),
                )

                tech_ap_text = getattr(acc, "tech_ap", "")
                tech_ap_val = _parse_float(tech_ap_text)
                self.table.setItem(row, COL_TECH_AP, NumericTableWidgetItem(tech_ap_text, tech_ap_val))

                avg_acc_val = _parse_float(acc.average_acc)
                self.table.setItem(row, COL_AVG_ACC, NumericTableWidgetItem(acc.average_acc, avg_acc_val))

                plays_val = _parse_int(acc.plays)
                self.table.setItem(row, COL_PLAYS, NumericTableWidgetItem(acc.plays, plays_val))
            else:
                for col in [
                    COL_AP,
                    COL_TRUE_AP,
                    COL_STANDARD_AP,
                    COL_TECH_AP,
                    COL_AVG_ACC,
                    COL_PLAYS,
                ]:
                    self.table.setItem(row, col, NumericTableWidgetItem("", None))

            # ScoreSaber 列 (必ず存在する前提)
            ss_pp_text = f"{ss.pp:.2f}"
            self.table.setItem(row, COL_SS_PP, NumericTableWidgetItem(ss_pp_text, ss.pp))




            self.table.setItem(
                row,
                COL_SS_GLOBAL_RANK,
                NumericTableWidgetItem(str(ss.global_rank), ss.global_rank),
            )
            self.table.setItem(
                row,
                COL_SS_COUNTRY_RANK,
                NumericTableWidgetItem(str(ss.country_rank), ss.country_rank),
            )

            # BeatLeader 列
            # BL PP / BL Global Rank は BeatLeader 情報があれば常に表示し、
            # BL Country Rank だけは「国フィルタと一致する場合のみ」表示する。
            if bl is not None:
                if bl.id:
                    attached_bl_ids.add(bl.id)
                bl_pp_text = f"{bl.pp:.2f}"
                self.table.setItem(row, COL_BL_PP, NumericTableWidgetItem(bl_pp_text, bl.pp))
                


                self.table.setItem(
                    row,
                    COL_BL_GLOBAL_RANK,
                    NumericTableWidgetItem(str(bl.global_rank), bl.global_rank),
                )

                if country is None or bl_ok:
                    self.table.setItem(
                        row,
                        COL_BL_COUNTRY_RANK,
                        NumericTableWidgetItem(str(bl.country_rank), bl.country_rank),
                    )
                else:
                    # 他国の Country Rank は混乱を招くので空欄にする
                    self.table.setItem(row, COL_BL_COUNTRY_RANK, NumericTableWidgetItem("", None))
            else:
                    self.table.setItem(row, COL_BL_PP, NumericTableWidgetItem("", None))
                    self.table.setItem(row, COL_BL_GLOBAL_RANK, NumericTableWidgetItem("", None))
                    self.table.setItem(row, COL_BL_COUNTRY_RANK, NumericTableWidgetItem("", None))

        # 次に、「ScoreSaber 行に紐づかなかった BeatLeader プレイヤー」だけを
        # beatleader_xxRanking.json の内容に基づいて追加する。
        # これにより、対象国の BeatLeader ランキングが「歯抜け」にならず、
        # かつ SS/BL の両方に存在するプレイヤーの行が二重に追加されない。
        for bl in self.bl_players.values():
            sid = bl.id
            if not sid:
                continue

            # 既に ScoreSaber ベースの行に紐づいている BeatLeader プレイヤーはスキップ
            if sid in attached_bl_ids:
                continue

            # 国フィルタが指定されている場合は BeatLeader 側の国コードで絞る
            if country is not None:
                target = country.upper()
                if not bl.country or bl.country.upper() != target:
                    continue

            row = self.table.rowCount()
            self.table.insertRow(row)

            # AccSaber 情報を scoresaber_id（= BL の sid と同じ）で引く
            acc_bl: Optional[AccSaberPlayer] = acc_by_sid.get(sid)

            if acc_bl is not None:
                self.table.setItem(row, COL_ACC_RANK, NumericTableWidgetItem(str(acc_bl.rank), acc_bl.rank))

                t_rank = true_rank_by_sid.get(sid)
                self.table.setItem(row, COL_TRUE_ACC_RANK, NumericTableWidgetItem("" if t_rank is None else str(t_rank), t_rank))

                s_rank = standard_rank_by_sid.get(sid)
                self.table.setItem(row, COL_STANDARD_ACC_RANK, NumericTableWidgetItem("" if s_rank is None else str(s_rank), s_rank))

                te_rank = tech_rank_by_sid.get(sid)
                self.table.setItem(row, COL_TECH_ACC_RANK, NumericTableWidgetItem("" if te_rank is None else str(te_rank), te_rank))

                acc_cr_val = acc_country_rank.get(sid)
                self.table.setItem(row, COL_ACC_COUNTRY_RANK, NumericTableWidgetItem("" if acc_cr_val is None else str(acc_cr_val), acc_cr_val))

                t_cr = true_country_rank_by_sid.get(sid)
                self.table.setItem(row, COL_TRUE_ACC_COUNTRY_RANK, NumericTableWidgetItem("" if t_cr is None else str(t_cr), t_cr))

                s_cr = standard_country_rank_by_sid.get(sid)
                self.table.setItem(row, COL_STANDARD_ACC_COUNTRY_RANK, NumericTableWidgetItem("" if s_cr is None else str(s_cr), s_cr))

                te_cr = tech_country_rank_by_sid.get(sid)
                self.table.setItem(row, COL_TECH_ACC_COUNTRY_RANK, NumericTableWidgetItem("" if te_cr is None else str(te_cr), te_cr))
            else:
                # AccSaber 情報は不明なので空欄
                for col in [
                    COL_ACC_RANK,
                    COL_TRUE_ACC_RANK,
                    COL_STANDARD_ACC_RANK,
                    COL_TECH_ACC_RANK,
                    COL_ACC_COUNTRY_RANK,
                    COL_TRUE_ACC_COUNTRY_RANK,
                    COL_STANDARD_ACC_COUNTRY_RANK,
                    COL_TECH_ACC_COUNTRY_RANK,
                ]:
                    self.table.setItem(row, col, NumericTableWidgetItem("", None))

            # Player / Country
            player_item = TextTableWidgetItem(bl.name)
            player_item.setData(Qt.ItemDataRole.UserRole, sid)
            self.table.setItem(row, COL_PLAYER, player_item)

            country_text = bl.country.upper() if bl.country else (country.upper() if country else "")
            self.table.setItem(row, COL_COUNTRY, TextTableWidgetItem(country_text))

            # AP 等も ACC と同様に引けた場合は表示
            if acc_bl is not None:
                total_ap_val = _parse_float(acc_bl.total_ap)
                self.table.setItem(row, COL_AP, NumericTableWidgetItem(acc_bl.total_ap, total_ap_val))

                true_ap_text = getattr(acc_bl, "true_ap", "")
                self.table.setItem(row, COL_TRUE_AP, NumericTableWidgetItem(true_ap_text, _parse_float(true_ap_text)))

                standard_ap_text = getattr(acc_bl, "standard_ap", "")
                self.table.setItem(row, COL_STANDARD_AP, NumericTableWidgetItem(standard_ap_text, _parse_float(standard_ap_text)))

                tech_ap_text = getattr(acc_bl, "tech_ap", "")
                self.table.setItem(row, COL_TECH_AP, NumericTableWidgetItem(tech_ap_text, _parse_float(tech_ap_text)))

                avg_acc_val = _parse_float(acc_bl.average_acc)
                self.table.setItem(row, COL_AVG_ACC, NumericTableWidgetItem(acc_bl.average_acc, avg_acc_val))

                plays_val = _parse_int(acc_bl.plays)
                self.table.setItem(row, COL_PLAYS, NumericTableWidgetItem(acc_bl.plays, plays_val))
            else:
                # AP 等も不明なので空欄
                for col in [
                    COL_AP,
                    COL_TRUE_AP,
                    COL_STANDARD_AP,
                    COL_TECH_AP,
                    COL_AVG_ACC,
                    COL_PLAYS,
                ]:
                    self.table.setItem(row, col, NumericTableWidgetItem("", None))

            # ScoreSaber 列は空
            for col in [COL_SS_PP, COL_SS_PLAYS, COL_SS_GLOBAL_RANK, COL_SS_COUNTRY_RANK]:
                self.table.setItem(row, col, NumericTableWidgetItem("", None))

            # BeatLeader 列は JSON の値をそのまま表示
            bl_pp_text = f"{bl.pp:.2f}"
            self.table.setItem(row, COL_BL_PP, NumericTableWidgetItem(bl_pp_text, bl.pp))
            self.table.setItem(
                row,
                COL_BL_GLOBAL_RANK,
                NumericTableWidgetItem(str(bl.global_rank), bl.global_rank),
            )

            # 国フィルタと一致する場合だけ Country Rank を表示（グローバル時はそのまま）
            if country is None or (bl.country and bl.country.upper() == country.upper()):
                self.table.setItem(
                    row,
                    COL_BL_COUNTRY_RANK,
                    NumericTableWidgetItem(str(bl.country_rank), bl.country_rank),
                )
            else:
                self.table.setItem(row, COL_BL_COUNTRY_RANK, NumericTableWidgetItem("", None))

        # もとのソート設定を復元し、ScoreSaber Global Rank 昇順で並べておく
        if was_sorting_enabled:
            self.table.setSortingEnabled(True)
            # SS Global Rank 列でソート
            self.table.sortItems(COL_SS_GLOBAL_RANK)

    def focus_on_steam_id(self, steam_id: str) -> None:
        """指定した SteamID を持つ行を探してテーブル中央付近にスクロールする。"""

        sid = steam_id.strip()
        if not sid:
            return

        highlight_brush = QBrush(QColor("#0b64c6"))
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COL_PLAYER)
            if item is None:
                continue
            val = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(val, str) and val == sid:
                self.table.selectRow(row)
                self.table.scrollToItem(item, QTableWidget.ScrollHint.PositionAtCenter)
                item.setForeground(highlight_brush)
                break


def run() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(1880, 1024)
    window.show()
    sys.exit(app.exec())
