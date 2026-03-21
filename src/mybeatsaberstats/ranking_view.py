from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
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
        raw = json.loads(path.read_text(encoding="utf-8"))
        # 新形式: {"fetched_at": ..., "data": [...]}
        if isinstance(raw, dict):
            data = raw.get("data") or []
        else:
            data = raw  # 旧形式: plain list
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
        # 新形式: {"fetched_at": ..., "rows": [...]}
        if isinstance(raw, dict):
            raw = raw.get("rows") or []
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


def _parse_ap(val: str) -> float:
    try:
        return float(str(val).replace(",", "")) if val else 0.0
    except (ValueError, TypeError):
        return 0.0


class RankingDialog(QDialog):
    """ScoreSaber / BeatLeader / AccSaber のランキングを統合表示するダイアログ。"""

    # Column indices
    _COL_PLAYER = 0
    _COL_COUNTRY = 1
    _COL_SS_PP = 2
    _COL_SS_RANK = 3
    _COL_SS_CRANK = 4
    _COL_BL_PP = 5
    _COL_BL_RANK = 6
    _COL_BL_CRANK = 7
    _COL_ACC_AP = 8
    _COL_ACC_RANK = 9
    _COL_ACC_CRANK = 10
    _COL_TRUE_AP = 11
    _COL_TRUE_RANK = 12
    _COL_TRUE_CRANK = 13
    _COL_STD_AP = 14
    _COL_STD_RANK = 15
    _COL_STD_CRANK = 16
    _COL_TECH_AP = 17
    _COL_TECH_RANK = 18
    _COL_TECH_CRANK = 19
    _COL_AVG_ACC = 20
    _COL_PLAYS = 21
    _NUM_COLS = 22

    def __init__(self, parent=None, steam_id: Optional[str] = None) -> None:  # type: ignore[override]
        super().__init__(parent)
        self.setWindowTitle("Ranking")
        self.resize(1600, 600)

        self._steam_id_to_highlight = steam_id

        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, self._NUM_COLS, self)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setHorizontalHeaderLabels([
            "Player",       # 0
            "Country",      # 1
            "SS PP",        # 2
            "SS 🌐",        # 3
            "SS 🏠",        # 4
            "BL PP",        # 5
            "BL 🌐",        # 6
            "BL 🏠",        # 7
            "AP",           # 8
            "AP 🌐",        # 9
            "AP 🏠",        # 10
            "True AP",      # 11
            "True 🌐",      # 12
            "True 🏠",      # 13
            "Std AP",       # 14
            "Std 🌐",       # 15
            "Std 🏠",       # 16
            "Tech AP",      # 17
            "Tech 🌐",      # 18
            "Tech 🏠",      # 19
            "AvgACC",       # 20
            "Plays",        # 21
        ])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        self.table.setSortingEnabled(True)

        layout.addWidget(self.table)

        self._load_and_populate()

    # --------------- internal helpers ---------------

    def _load_and_populate(self) -> None:
        ss_path = CACHE_DIR / "scoresaber_ranking.json"
        acc_path = CACHE_DIR / "accsaber_ranking.json"
        bl_path = CACHE_DIR / "beatleader_ranking.json"
        pl_counts_path = CACHE_DIR / "accsaber_playlist_counts.json"

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

        # BeatLeader: beatleader_ranking.json を優先し、不足分を players_index.json で補完
        bl_by_sid: Dict[str, BeatLeaderPlayer] = {}
        if bl_path.exists():
            bl_players: List[BeatLeaderPlayer] = _load_list_cache(bl_path, BeatLeaderPlayer)
            for p in bl_players:
                if p.id:
                    bl_by_sid[str(p.id)] = p
        index = _load_player_index()
        for sid, entry in index.items():
            if sid not in bl_by_sid:
                bl_obj = entry.get("beatleader")
                if isinstance(bl_obj, BeatLeaderPlayer):
                    bl_by_sid[sid] = bl_obj

        # AccSaber プレイリスト総マップ数
        total_acc_maps: Optional[int] = None
        if pl_counts_path.exists():
            try:
                pl_data = json.loads(pl_counts_path.read_text(encoding="utf-8"))
                total_acc_maps = sum(
                    pl_data[k]["count"]
                    for k in ("true", "standard", "tech")
                    if isinstance(pl_data.get(k), dict) and isinstance(pl_data[k].get("count"), int)
                )
            except Exception:  # noqa: BLE001
                pass

        # AccSaber を scoresaber_id でインデックス
        acc_by_ssid: Dict[str, AccSaberPlayer] = {}
        for p in acc_players:
            sid = getattr(p, "scoresaber_id", None)
            if not sid:
                continue
            acc_by_ssid[str(sid)] = p

        # ホーム国プレイヤーの SID セット（SS ランキングに含まれる全員）
        home_sids: set[str] = {ss.id for ss in ss_players}

        # AccSaber カテゴリ別グローバルランクを計算（全プレイヤー中の順位）
        def _global_ranks(attr: str) -> Dict[str, int]:
            sorted_p = sorted(
                acc_players,
                key=lambda p: _parse_ap(getattr(p, attr, "")),
                reverse=True,
            )
            return {
                str(p.scoresaber_id): i + 1
                for i, p in enumerate(sorted_p)
                if p.scoresaber_id and _parse_ap(getattr(p, attr, "")) > 0
            }

        # AccSaber カテゴリ別国内ランクを計算（ホーム国プレイヤー中の順位）
        def _country_ranks(attr: str) -> Dict[str, int]:
            home_list = [
                p for p in acc_players
                if p.scoresaber_id and str(p.scoresaber_id) in home_sids
            ]
            sorted_p = sorted(
                home_list,
                key=lambda p: _parse_ap(getattr(p, attr, "")),
                reverse=True,
            )
            return {
                str(p.scoresaber_id): i + 1
                for i, p in enumerate(sorted_p)
                if _parse_ap(getattr(p, attr, "")) > 0
            }

        acc_total_crank = _country_ranks("total_ap")
        acc_true_grank = _global_ranks("true_ap")
        acc_true_crank = _country_ranks("true_ap")
        acc_std_grank = _global_ranks("standard_ap")
        acc_std_crank = _country_ranks("standard_ap")
        acc_tech_grank = _global_ranks("tech_ap")
        acc_tech_crank = _country_ranks("tech_ap")

        N = NumericTableWidgetItem  # shorthand

        self.table.setRowCount(0)

        for ss in ss_players:
            sid = ss.id
            if not sid:
                continue

            bl = bl_by_sid.get(sid)
            acc = acc_by_ssid.get(sid)

            row = self.table.rowCount()
            self.table.insertRow(row)

            # Player (col 0)
            item_player = QTableWidgetItem(ss.name)
            item_player.setData(Qt.ItemDataRole.UserRole, sid)
            self.table.setItem(row, self._COL_PLAYER, item_player)

            # Country (col 1)
            self.table.setItem(row, self._COL_COUNTRY, QTableWidgetItem(ss.country.upper() if ss.country else ""))

            # SS (cols 2-4)
            self.table.setItem(row, self._COL_SS_PP, N(f"{ss.pp:,.2f}", ss.pp))
            self.table.setItem(row, self._COL_SS_RANK, N(f"{ss.global_rank:,}", ss.global_rank))
            self.table.setItem(row, self._COL_SS_CRANK, N(f"{ss.country_rank:,}", ss.country_rank))

            # BL (cols 5-7)
            if bl is not None:
                self.table.setItem(row, self._COL_BL_PP, N(f"{bl.pp:,.2f}", bl.pp))
                self.table.setItem(row, self._COL_BL_RANK, N(f"{bl.global_rank:,}", bl.global_rank))
                self.table.setItem(row, self._COL_BL_CRANK, N(f"{bl.country_rank:,}", bl.country_rank))
            else:
                for c in (self._COL_BL_PP, self._COL_BL_RANK, self._COL_BL_CRANK):
                    self.table.setItem(row, c, N("", None))

            # AccSaber (cols 8-21)
            if acc is not None:
                ap_total = _parse_ap(acc.total_ap)
                self.table.setItem(row, self._COL_ACC_AP, N(f"{ap_total:,.2f}", ap_total))
                self.table.setItem(row, self._COL_ACC_RANK, N(f"{acc.rank:,}", acc.rank))
                acc_cr = acc_total_crank.get(sid)
                self.table.setItem(row, self._COL_ACC_CRANK, N(f"{acc_cr:,}" if acc_cr else "", acc_cr))

                true_ap = _parse_ap(acc.true_ap)
                self.table.setItem(row, self._COL_TRUE_AP, N(f"{true_ap:,.2f}" if true_ap > 0 else "", true_ap if true_ap > 0 else None))
                true_gr = acc_true_grank.get(sid)
                self.table.setItem(row, self._COL_TRUE_RANK, N(f"{true_gr:,}" if true_gr else "", true_gr))
                true_cr = acc_true_crank.get(sid)
                self.table.setItem(row, self._COL_TRUE_CRANK, N(f"{true_cr:,}" if true_cr else "", true_cr))

                std_ap = _parse_ap(acc.standard_ap)
                self.table.setItem(row, self._COL_STD_AP, N(f"{std_ap:,.2f}" if std_ap > 0 else "", std_ap if std_ap > 0 else None))
                std_gr = acc_std_grank.get(sid)
                self.table.setItem(row, self._COL_STD_RANK, N(f"{std_gr:,}" if std_gr else "", std_gr))
                std_cr = acc_std_crank.get(sid)
                self.table.setItem(row, self._COL_STD_CRANK, N(f"{std_cr:,}" if std_cr else "", std_cr))

                tech_ap = _parse_ap(acc.tech_ap)
                self.table.setItem(row, self._COL_TECH_AP, N(f"{tech_ap:,.2f}" if tech_ap > 0 else "", tech_ap if tech_ap > 0 else None))
                tech_gr = acc_tech_grank.get(sid)
                self.table.setItem(row, self._COL_TECH_RANK, N(f"{tech_gr:,}" if tech_gr else "", tech_gr))
                tech_cr = acc_tech_crank.get(sid)
                self.table.setItem(row, self._COL_TECH_CRANK, N(f"{tech_cr:,}" if tech_cr else "", tech_cr))

                # Avg ACC
                try:
                    avg_acc = float(str(acc.average_acc).replace(",", "").replace("%", ""))
                    if avg_acc < 1.0:
                        avg_acc *= 100.0
                    self.table.setItem(row, self._COL_AVG_ACC, N(f"{avg_acc:.2f}%", avg_acc))
                except (ValueError, TypeError):
                    self.table.setItem(row, self._COL_AVG_ACC, QTableWidgetItem(str(acc.average_acc)))

                # Plays
                try:
                    plays = int(str(acc.plays).replace(",", ""))
                    plays_str = f"{plays:,}/{total_acc_maps:,}" if total_acc_maps is not None else f"{plays:,}"
                    self.table.setItem(row, self._COL_PLAYS, N(plays_str, plays))
                except (ValueError, TypeError):
                    self.table.setItem(row, self._COL_PLAYS, QTableWidgetItem(str(acc.plays)))
            else:
                for c in range(self._COL_ACC_AP, self._NUM_COLS):
                    self.table.setItem(row, c, N("", None))

        # SS グローバルランク昇順でソート
        if self.table.rowCount() > 0:
            self.table.sortItems(self._COL_SS_RANK, Qt.SortOrder.AscendingOrder)

        # 自分の行へスクロール
        if self._steam_id_to_highlight:
            self._scroll_to_steam_id(self._steam_id_to_highlight)

    def _scroll_to_steam_id(self, steam_id: str) -> None:
        sid = steam_id.strip()
        if not sid:
            return

        for row in range(self.table.rowCount()):
            item = self.table.item(row, self._COL_PLAYER)
            if item is None:
                continue
            val = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(val, str) and val == sid:
                self.table.selectRow(row)
                self.table.scrollToItem(item, QTableWidget.ScrollHint.PositionAtCenter)
                break
