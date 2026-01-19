from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QMessageBox,
)

from .snapshot import BASE_DIR
from .scoresaber import ScoreSaberPlayer
from .accsaber import AccSaberPlayer
from .beatleader import BeatLeaderPlayer


CACHE_DIR = BASE_DIR / "cache"


class NumericTableWidgetItem(QTableWidgetItem):
    """数値としてソートしたい列用のアイテム。"""

    def __init__(self, text: str, sort_value: float | int | None = None) -> None:
        super().__init__(text)
        self._sort_value = sort_value

    def __lt__(self, other: "QTableWidgetItem") -> bool:  # type: ignore[override]
        if isinstance(other, NumericTableWidgetItem):
            a = self._sort_value
            b = other._sort_value
            if a is not None and b is not None:
                return a < b
        return super().__lt__(other)


def _load_list_cache(path: Path, cls):
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [cls(**item) for item in data if isinstance(item, dict)]
    except Exception:  # noqa: BLE001
        return []
    return []


def _load_player_index() -> Dict[str, Dict[str, object]]:
    """players_index.json を読み込んで辞書形式で返す。壊れていれば空 dict。"""

    path = CACHE_DIR / "players_index.json"
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        index: Dict[str, Dict[str, object]] = {}
        if isinstance(raw, list):
            for row in raw:
                if not isinstance(row, dict):
                    continue
                sid = str(row.get("steam_id") or "")
                if not sid:
                    continue
                entry: Dict[str, object] = {}
                ss = row.get("scoresaber")
                bl = row.get("beatleader")
                if isinstance(ss, dict):
                    try:
                        entry["scoresaber"] = ScoreSaberPlayer(**ss)
                    except TypeError:
                        pass
                if isinstance(bl, dict):
                    try:
                        entry["beatleader"] = BeatLeaderPlayer(**bl)
                    except TypeError:
                        pass
                if entry:
                    index[sid] = entry
        return index
    except Exception:  # noqa: BLE001
        return {}


class RankingDialog(QDialog):
    """ScoreSaber / BeatLeader / AccSaber のランキングを統合表示するダイアログ。"""

    def __init__(self, parent=None, steam_id: Optional[str] = None) -> None:  # type: ignore[override]
        super().__init__(parent)
        self.setWindowTitle("Ranking")
        self.resize(1000, 600)

        self._steam_id_to_highlight = steam_id

        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 11, self)
        self.table.setHorizontalHeaderLabels([
            "SS Rank",          # 0
            "Player",           # 1
            "Country",          # 2
            "SS PP",            # 3
            "BL PP",            # 4
            "BL Global Rank",   # 5
            "BL Country Rank",  # 6
            "ACC Rank",         # 7
            "ACC AP",           # 8
            "ACC Avg ACC",      # 9
            "ACC Plays",        # 10
        ])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.setSortingEnabled(True)

        layout.addWidget(self.table)

        self._load_and_populate()

    # --------------- internal helpers ---------------

    def _load_and_populate(self) -> None:
        ss_path = CACHE_DIR / "scoresaber_ranking.json"
        acc_path = CACHE_DIR / "accsaber_ranking.json"

        if not ss_path.exists() or not acc_path.exists():
            QMessageBox.warning(
                self,
                "Ranking",
                "ランキングキャッシュ(scoresaber_ranking.json / accsaber_ranking.json)が見つかりません。\n"
                "Stats画面の \"Fetch Ranking Data\" ボタンで取得してから再度お試しください。",
            )
            return

        ss_players: List[ScoreSaberPlayer] = _load_list_cache(ss_path, ScoreSaberPlayer)
        acc_players: List[AccSaberPlayer] = _load_list_cache(acc_path, AccSaberPlayer)

        # AccSaber は scoresaber_id で引けるようにしておく
        acc_by_ssid: Dict[str, AccSaberPlayer] = {}
        for p in acc_players:
            sid = getattr(p, "scoresaber_id", None)
            if not sid:
                continue
            acc_by_ssid[str(sid)] = p

        # BeatLeader は players_index.json から ScoreSaber ID ごとに引く
        index = _load_player_index()
        bl_by_sid: Dict[str, BeatLeaderPlayer] = {}
        for sid, entry in index.items():
            bl_obj = entry.get("beatleader")
            if isinstance(bl_obj, BeatLeaderPlayer):
                bl_by_sid[sid] = bl_obj

        self.table.setRowCount(0)

        for ss in ss_players:
            sid = ss.id
            if not sid:
                continue

            acc = acc_by_ssid.get(sid)
            bl = bl_by_sid.get(sid)

            row = self.table.rowCount()
            self.table.insertRow(row)

            # SS Rank (数値ソート)
            self.table.setItem(row, 0, NumericTableWidgetItem(str(ss.global_rank), ss.global_rank))

            # Player 名 + SteamID を UserRole に入れておく
            player_name = ss.name
            item_player = QTableWidgetItem(player_name)
            item_player.setData(Qt.ItemDataRole.UserRole, sid)
            self.table.setItem(row, 1, item_player)

            # Country
            self.table.setItem(row, 2, QTableWidgetItem(ss.country.upper() if ss.country else ""))

            # SS PP
            ss_pp_text = f"{ss.pp:.2f}"
            self.table.setItem(row, 3, NumericTableWidgetItem(ss_pp_text, ss.pp))

            # BeatLeader 列
            if bl is not None:
                bl_pp_text = f"{bl.pp:.2f}"
                self.table.setItem(row, 4, NumericTableWidgetItem(bl_pp_text, bl.pp))
                self.table.setItem(row, 5, NumericTableWidgetItem(str(bl.global_rank), bl.global_rank))
                self.table.setItem(row, 6, NumericTableWidgetItem(str(bl.country_rank), bl.country_rank))
            else:
                self.table.setItem(row, 4, NumericTableWidgetItem("", None))
                self.table.setItem(row, 5, NumericTableWidgetItem("", None))
                self.table.setItem(row, 6, NumericTableWidgetItem("", None))

            # AccSaber 列
            if acc is not None:
                self.table.setItem(row, 7, NumericTableWidgetItem(str(acc.rank), acc.rank))
                self.table.setItem(row, 8, QTableWidgetItem(acc.total_ap))
                self.table.setItem(row, 9, QTableWidgetItem(acc.average_acc))
                self.table.setItem(row, 10, QTableWidgetItem(acc.plays))
            else:
                self.table.setItem(row, 7, NumericTableWidgetItem("", None))
                self.table.setItem(row, 8, QTableWidgetItem(""))
                self.table.setItem(row, 9, QTableWidgetItem(""))
                self.table.setItem(row, 10, QTableWidgetItem(""))

        # ScoreSaber ランク昇順でソート
        if self.table.rowCount() > 0:
            self.table.sortItems(0, Qt.SortOrder.AscendingOrder)

        # 自分の行へスクロール
        if self._steam_id_to_highlight:
            self._scroll_to_steam_id(self._steam_id_to_highlight)

    def _scroll_to_steam_id(self, steam_id: str) -> None:
        sid = steam_id.strip()
        if not sid:
            return

        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item is None:
                continue
            val = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(val, str) and val == sid:
                self.table.selectRow(row)
                self.table.scrollToItem(item, QTableWidget.ScrollHint.PositionAtCenter)
                break
