"""Playlist 画面 — ScoreSaber / BeatLeader / AccSaber / AccSaber Reloaded の
ランクマップ、または任意の .bplist ファイルを一覧表示してフィルタ・ソート・一括出力を行う画面。

AccSaber / AccSaber Reloaded は API から取得するためネットワーク接続が必要。
"""
from __future__ import annotations

import base64
import json
import math
import os
import tempfile
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import requests

from PySide6.QtCore import Qt, QObject, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QFont, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QButtonGroup,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QProgressDialog,
    QApplication,
    QCheckBox,
)

from .snapshot import BASE_DIR, RESOURCES_DIR
from .theme import is_dark, table_stylesheet

# ──────────────────────────────────────────────────────────────────────────────
# ソース定数
# ──────────────────────────────────────────────────────────────────────────────
SOURCE_SS = "ScoreSaber"
SOURCE_BL = "BeatLeader"
SOURCE_ACC = "AccSaber"
SOURCE_ACC_RL = "AccSaber RL"
SOURCE_OPEN = "Open File"

# ステータス表示
STATUS_CLEARED = "✔"
STATUS_NF = "⚠NF"
STATUS_WARN = "⚠"    # NF 以外のモディファイアによる未公認クリア
STATUS_UNPLAYED = "✖"

# 難易度の表示順
_DIFF_ORDER: Dict[str, int] = {"Easy": 1, "Normal": 2, "Hard": 3, "Expert": 4, "ExpertPlus": 5}

_CACHE_DIR = BASE_DIR / "cache"
_BATCH_CONFIG_PATH = _CACHE_DIR / "batch_configs.json"
_EXPORT_DIR_PATH = _CACHE_DIR / "export_dir.json"
_PLAYLIST_WINDOW_PATH = _CACHE_DIR / "playlist_window.json"


# ──────────────────────────────────────────────────────────────────────────────
# データクラス
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class MapEntry:
    """プレイリスト画面の 1 行を表す。"""
    song_name: str
    song_author: str
    mapper: str
    song_hash: str          # 大文字ハッシュ
    difficulty: str         # ExpertPlus など
    mode: str               # Standard など
    stars: float
    max_pp: float           # SS: maxPP (多くは 0), BL: 0 (starsを代替使用)
    player_pp: float        # プレイヤーの取得 PP (0 = 未プレイ)
    cleared: bool           # NF/SS/NA なしクリア済み
    nf_clear: bool          # NF 付きクリアあり (cleared=False の場合)
    player_acc: float       # プレイヤーの精度 % (0 = 未プレイ)
    player_rank: int        # プレイヤーのランク (0 = 未プレイ)
    leaderboard_id: str     # SS: leaderboard id, BL: map id
    source: str             # "scoresaber" | "beatleader" | "open"
    acc_category: str = ""  # AccSaber / AccSaber Reloaded のカテゴリ (true/standard/tech)
    acc_rl_ap: float = 0.0  # AccSaber Reloaded AP (0 = 未取得 / 未プレイ)
    acc_complexity: float = 0.0  # AccSaber / AccSaber Reloaded の Complexity
    player_mods: str = ""   # 実際に使用したモディファイア文字列 (例: "NF", "SC", "NF,SC")

    @property
    def status_str(self) -> str:
        if self.cleared:
            return STATUS_CLEARED
        if self.nf_clear:
            mods_upper = self.player_mods.upper()
            if not self.player_mods or "NF" in mods_upper:
                return STATUS_NF
            # SC 等の NF 以外のモディファイアで無効化されている場合
            return f"{STATUS_WARN}{self.player_mods}"
        return STATUS_UNPLAYED

    @property
    def sort_stars(self) -> float:
        return self.stars

    @property
    def played(self) -> bool:
        return self.cleared or self.nf_clear


# ──────────────────────────────────────────────────────────────────────────────
# ヘルパー
# ──────────────────────────────────────────────────────────────────────────────

def _diff_from_raw(raw_str: str, diff_num: int = 0) -> str:
    """SS の difficultyRaw / difficulty 番号から表示名へ変換。"""
    if raw_str:
        for pat, name in [
            ("_ExpertPlus_", "ExpertPlus"),
            ("_Expert_", "Expert"),
            ("_Hard_", "Hard"),
            ("_Normal_", "Normal"),
            ("_Easy_", "Easy"),
        ]:
            if pat in raw_str:
                return name
    _num_map = {1: "Easy", 3: "Normal", 5: "Hard", 7: "Expert", 9: "ExpertPlus"}
    return _num_map.get(diff_num, str(diff_num))


def _mode_from_gamemode(game_mode: str) -> str:
    """SoloStandard → Standard"""
    return game_mode.replace("Solo", "") if game_mode else "Standard"


# ──────────────────────────────────────────────────────────────────────────────
# データ読み込み
# ──────────────────────────────────────────────────────────────────────────────

def _ss_player_score_info(scores: Dict, lb_id: str, max_score_from_map: int = 0
                          ) -> Tuple[float, bool, bool, float, int, str]:
    """SS player scores から (player_pp, cleared, nf_clear, acc, rank, mods) を返す。"""
    entry = scores.get(str(lb_id))
    if not entry:
        return 0.0, False, False, 0.0, 0, ""
    sc = entry.get("score", {})
    player_pp = float(sc.get("pp") or 0)
    base_score = int(sc.get("baseScore") or 0)
    modifiers = (sc.get("modifiers") or "").upper()
    # SS modifiers は "NFSC" のように連結されているので 2 文字ずつカンマ区切りに正規化
    mods_str = ",".join(modifiers[i:i+2] for i in range(0, len(modifiers), 2)) if modifiers else ""
    rank = int(sc.get("rank") or 0)
    # 精度算出 (max_score が 0 の場合は 0%)
    acc = (base_score / max_score_from_map * 100.0) if max_score_from_map > 0 and base_score > 0 else 0.0
    has_nf = "NF" in modifiers
    cleared = base_score > 0 and not has_nf
    nf_clear = base_score > 0 and has_nf
    return player_pp, cleared, nf_clear, acc, rank, mods_str


def _bl_player_score_info(scores: Dict, map_id: str
                          ) -> Tuple[float, bool, bool, float, int, str]:
    """BL player scores から (player_pp, cleared, nf_clear, acc, rank, mods) を返す。"""
    entry = scores.get(str(map_id))
    if not entry:
        return 0.0, False, False, 0.0, 0, ""
    player_pp = float(entry.get("pp") or 0)
    base_score = int(entry.get("baseScore") or 0)
    modifiers = (entry.get("modifiers") or "").upper()
    # BL modifiers は "NF,SC" 形式だが念のため正規化
    mods_str = ",".join(m.strip() for m in modifiers.replace(",", " ").split() if m.strip()) if modifiers else ""
    accuracy = float(entry.get("accuracy") or 0)
    rank = int(entry.get("rank") or 0)
    acc = accuracy * 100.0 if accuracy else 0.0
    has_nf = "NF" in modifiers
    cleared = base_score > 0 and not has_nf
    nf_clear = base_score > 0 and has_nf
    return player_pp, cleared, nf_clear, acc, rank, mods_str


def load_ss_maps(steam_id: Optional[str] = None, filter_stars: bool = True) -> List[MapEntry]:
    """ScoreSaber ランクマップをキャッシュから読み込む。

    filter_stars=False にすると stars=0 のマップも返す（AccSaber 用）。
    """
    path = _CACHE_DIR / "scoresaber_ranked_maps.json"
    if not path.exists():
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    # トップレベル構造: {fetched_at: ..., leaderboards: {lb_id: {...}, ...}, ...}
    if "leaderboards" in raw:
        maps_dict: Dict[str, dict] = raw["leaderboards"]
    else:
        # 旧フォーマット互換: トップレベルに直接マップがある場合
        maps_dict = {
            k: v for k, v in raw.items()
            if k not in ("fetched_at", "max_pages", "total_maps") and isinstance(v, dict)
        }

    # プレイヤースコアを読み込む
    ss_scores: Dict[str, dict] = {}
    if steam_id:
        sp = _CACHE_DIR / f"scoresaber_player_scores_{steam_id}.json"
        if sp.exists():
            try:
                sd = json.loads(sp.read_text(encoding="utf-8"))
                ss_scores = sd.get("scores", {})
            except Exception:
                pass

    entries: List[MapEntry] = []
    for lb_id_str, m in maps_dict.items():
        stars = float(m.get("stars") or 0)
        if filter_stars and stars <= 0:
            continue

        diff_info = m.get("difficulty", {})
        diff_num = int(diff_info.get("difficulty") or 0)
        diff_raw = diff_info.get("difficultyRaw") or ""
        game_mode = diff_info.get("gameMode") or "SoloStandard"
        max_score = int(m.get("maxScore") or 0)

        player_pp, cleared, nf_clear, acc, rank, mods = _ss_player_score_info(
            ss_scores, lb_id_str, max_score
        )

        entries.append(MapEntry(
            song_name=m.get("songName") or "",
            song_author=m.get("songAuthorName") or "",
            mapper=m.get("levelAuthorName") or "",
            song_hash=(m.get("songHash") or "").upper(),
            difficulty=_diff_from_raw(diff_raw, diff_num),
            mode=_mode_from_gamemode(game_mode),
            stars=stars,
            max_pp=float(m.get("maxPP") or 0),
            player_pp=player_pp,
            cleared=cleared,
            nf_clear=nf_clear,
            player_acc=acc,
            player_rank=rank,
            leaderboard_id=lb_id_str,
            source="scoresaber",
            player_mods=mods,
        ))

    return entries


def load_bl_maps(steam_id: Optional[str] = None) -> List[MapEntry]:
    """BeatLeader ランクマップをキャッシュから読み込む。"""
    path = _CACHE_DIR / "beatleader_ranked_maps.json"
    if not path.exists():
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []

    all_maps: List[dict] = []
    for page in raw.get("pages", []):
        page_data = page.get("data", {})
        if isinstance(page_data, dict):
            inner = page_data.get("data", [])
            if isinstance(inner, list):
                all_maps.extend(inner)

    # プレイヤースコアを読み込む
    bl_scores: Dict[str, dict] = {}
    if steam_id:
        bp = _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json"
        if bp.exists():
            try:
                bd = json.loads(bp.read_text(encoding="utf-8"))
                bl_scores = bd.get("scores", {})
            except Exception:
                pass

    entries: List[MapEntry] = []
    for m in all_maps:
        diff = m.get("difficulty", {})
        song = m.get("song", {})
        map_id = str(m.get("id") or "")
        stars = float(diff.get("stars") or 0)

        player_pp, cleared, nf_clear, acc, rank, mods = _bl_player_score_info(bl_scores, map_id)

        entries.append(MapEntry(
            song_name=song.get("name") or "",
            song_author=song.get("author") or "",
            mapper=song.get("mapper") or "",
            song_hash=(song.get("hash") or "").upper(),
            difficulty=diff.get("difficultyName") or "ExpertPlus",
            mode=diff.get("modeName") or "Standard",
            stars=stars,
            max_pp=0.0,
            player_pp=player_pp,
            cleared=cleared,
            nf_clear=nf_clear,
            player_acc=acc,
            player_rank=rank,
            leaderboard_id=map_id,
            source="beatleader",
            player_mods=mods,
        ))

    return entries


def _build_ss_score_hash_index(
    ss_scores: Dict[str, dict],
) -> Dict[Tuple[str, str, str], Tuple[float, bool, bool, float, int, str]]:
    """SS player scores キャッシュから (hash, mode, diff) → (pp, cleared, nf_clear, acc, rank, mods) を構築。

    SS ranked maps に含まれない Easy 等のマップもカバーするため AccSaber 用途で使用。
    """
    idx: Dict[Tuple[str, str, str], Tuple[float, bool, bool, float, int, str]] = {}
    for lb_id_str, entry in ss_scores.items():
        lb = entry.get("leaderboard", {})
        song_hash = (lb.get("songHash") or "").upper()
        if not song_hash:
            continue
        diff_info = lb.get("difficulty", {})
        diff_raw = diff_info.get("difficultyRaw") or ""
        diff_num = int(diff_info.get("difficulty") or 0)
        game_mode = diff_info.get("gameMode") or "SoloStandard"
        diff_name = _diff_from_raw(diff_raw, diff_num)
        mode = _mode_from_gamemode(game_mode)
        max_score = int(lb.get("maxScore") or 0)
        pp, cleared, nf_clear, acc, rank, mods = _ss_player_score_info(ss_scores, lb_id_str, max_score)
        key = (song_hash, mode, diff_name)
        # クリア済み優先、同キーに複数スコアがあれば最高 pp を保持
        if key not in idx or (cleared and not idx[key][1]) or pp > idx[key][0]:
            idx[key] = (pp, cleared, nf_clear, acc, rank, mods)
    return idx


def _build_ss_hash_index(entries: List[MapEntry]) -> Dict[Tuple[str, str, str], MapEntry]:
    """hash+mode+diff → MapEntry のインデックスを構築。"""
    idx: Dict[Tuple[str, str, str], MapEntry] = {}
    for e in entries:
        key = (e.song_hash.upper(), e.mode, e.difficulty)
        idx[key] = e
    return idx


def _build_bl_hash_index(entries: List[MapEntry]) -> Dict[Tuple[str, str, str], MapEntry]:
    return _build_ss_hash_index(entries)


def load_bplist_maps(
    bplist_path: Path,
    service: str,
    steam_id: Optional[str] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> List[MapEntry]:
    """開いた .bplist ファイルをロードし、service に応じてランク情報を付与する。

    service: "scoresaber" | "beatleader" | "accsaber_rl" | "none"
    on_progress: callable(done, total, label) — accsaber_rl 時のみ使用
    """
    try:
        bplist = json.loads(bplist_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"bplist load error: {e}") from e

    songs = bplist.get("songs") or bplist.get("Songs") or []

    if service == "scoresaber":
        ranked = load_ss_maps(steam_id)
        idx = _build_ss_hash_index(ranked)
    elif service == "beatleader":
        ranked = load_bl_maps(steam_id)
        idx = _build_bl_hash_index(ranked)
    elif service == "accsaber_rl":
        ranked = load_accsaber_reloaded_maps(steam_id, "all", on_progress=on_progress)
        idx = _build_ss_hash_index(ranked)
    elif service == "accsaber":
        ranked = load_accsaber_maps(steam_id, "all", on_progress=on_progress)
        idx = _build_ss_hash_index(ranked)
    else:
        idx = {}
        ranked = []

    entries: List[MapEntry] = []
    for song in songs:
        s_hash = (song.get("hash") or "").upper()
        s_name = song.get("songName") or ""
        diffs = song.get("difficulties") or []

        if not diffs:
            # 難易度指定なし → idx からハッシュで探す
            for e in (ranked or []):
                if e.song_hash == s_hash:
                    entries.append(e)
            if not ranked:
                entries.append(MapEntry(
                    song_name=s_name, song_author="", mapper="",
                    song_hash=s_hash, difficulty="", mode="",
                    stars=0.0, max_pp=0.0, player_pp=0.0,
                    cleared=False, nf_clear=False,
                    player_acc=0.0, player_rank=0,
                    leaderboard_id="", source="open",
                ))
        else:
            for d in diffs:
                char = d.get("characteristic") or "Standard"
                diff_name = d.get("name") or "ExpertPlus"
                key = (s_hash, char, diff_name)
                if key in idx:
                    entries.append(idx[key])
                else:
                    # ランク情報なし → 最低限の情報で登録
                    entries.append(MapEntry(
                        song_name=s_name, song_author="", mapper="",
                        song_hash=s_hash, difficulty=diff_name, mode=char,
                        stars=0.0, max_pp=0.0, player_pp=0.0,
                        cleared=False, nf_clear=False,
                        player_acc=0.0, player_rank=0,
                        leaderboard_id="", source="open",
                    ))

    return entries


# ──────────────────────────────────────────────────────────────────────────────
# 数値ソート対応アイテム
# ──────────────────────────────────────────────────────────────────────────────

class _NumItem(QTableWidgetItem):
    def __init__(self, text: str, sort_val: float = 0.0) -> None:
        super().__init__(text)
        self._v = sort_val

    def __lt__(self, other: "QTableWidgetItem") -> bool:  # type: ignore[override]
        other_v = other._v if isinstance(other, _NumItem) else 0.0  # type: ignore[attr-defined]
        return self._v < other_v


# 難易度アイコン: Beat Saber 公式カラー + 短縮テキスト
_DIFF_INFO: Dict[str, tuple] = {
    "Easy":       ("Es",  QColor("#1acc1a")),
    "Normal":     ("N",  QColor("#59b0f4")),
    "Hard":       ("H",  QColor("#f4a015")),
    "Expert":     ("Ex",   QColor("#ff4e4e")),
    "ExpertPlus": ("E+",  QColor("#bf2aff")),
}

# モードアイコン
_MODE_INFO: Dict[str, str] = {
    "Standard":  "2S",
    "OneSaber":  "1S",
    "NoArrows":  "NA",
    "90Degree":  "90°",
    "360Degree": "360°",
    "Lightshow": "LS",
    "Lawless":   "Law",
}


def _diff_item(difficulty: str) -> QTableWidgetItem:
    short, color = _DIFF_INFO.get(difficulty, (difficulty[:4], QColor("#aaaaaa")))
    item = QTableWidgetItem(short)
    item.setBackground(color)
    item.setForeground(QColor("#DDDDDD") if is_dark() else QColor("#000000"))
    item.setToolTip(difficulty)
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    # 太字にする（ただし、環境によってはフォントサイズが変わってしまうため、スタイルシートで擬似的に太字にする）
    font = item.font()
    font.setBold(True)
    item.setFont(font)
    return item


def _mode_item(mode: str) -> QTableWidgetItem:
    short = _MODE_INFO.get(mode, mode[:4])
    item = QTableWidgetItem(short)
    item.setToolTip(mode)
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    return item


def _sort_dir_from_mode(sort_mode: str) -> str:
    """sort_mode 文字列から 'asc'/'desc' を返す。"""
    return "desc" if sort_mode in ("pp_high", "ap_high", "acc_high", "rank_high", "star_desc") else "asc"


def _make_playlist_cover(
    cover_type: str,  # "star" | "true" | "standard" | "tech" | "default"
    label: str = "",  # "star" 時は星数文字列
    sort_dir: str = "asc",  # "asc" | "desc"
    source: str = "",  # "ss" | "bl" | "rl" | ""
) -> str:
    """プレイリストカバー画像を生成し data:image/png;base64,... を返す。

    cover_type:
        "star"     → SS: scoresaber_logo.svg / BL: beatleader_logo.jpg + ★N (黄)
        "true"     → accsaberreloaded_logo + Tr (緑)
        "standard" → accsaberreloaded_logo + St (青)
        "tech"     → accsaberreloaded_logo + Tc (赤)
        "default"  → SS/BL ロゴ or app_icon のみ
    sort_dir: "asc" → ⇧, "desc" → ⇩
    """
    SIZE = 256

    # ベース画像選択
    if cover_type in ("true", "standard", "tech"):
        base_path = RESOURCES_DIR / "accsaberreloaded_logo.png"
    elif source == "ss":
        base_path = RESOURCES_DIR / "scoresaber_logo.svg"
    elif source == "bl":
        base_path = RESOURCES_DIR / "beatleader_logo.jpg"
    elif source == "acc":
        base_path = RESOURCES_DIR / "asssaber_logo.webp"
    elif source == "rl":
        base_path = RESOURCES_DIR / "accsaberreloaded_logo.png"
    else:
        base_path = RESOURCES_DIR / "app_icon.png"

    if str(base_path).endswith(".svg") and base_path.exists():
        renderer = QSvgRenderer(str(base_path))
        base_img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
        base_img.fill(QColor(30, 30, 30))
        _svg_painter = QPainter(base_img)
        renderer.render(_svg_painter)
        _svg_painter.end()
    elif base_path.exists():
        base_img = QImage(str(base_path))
        base_img = base_img.scaled(
            SIZE, SIZE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        cx = (base_img.width() - SIZE) // 2
        cy = (base_img.height() - SIZE) // 2
        base_img = base_img.copy(cx, cy, SIZE, SIZE)
    else:
        base_img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32)
        base_img.fill(QColor(30, 30, 30))

    canvas = base_img.convertToFormat(QImage.Format.Format_ARGB32)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

    # カテゴリ・ラベル設定
    if cover_type == "true":
        main_text, text_color = "True", QColor(0, 220, 80)
    elif cover_type == "standard":
        main_text, text_color = "Std", QColor(80, 180, 255)
    elif cover_type == "tech":
        main_text, text_color = "Tech", QColor(255, 80, 80)
    elif cover_type == "star":
        main_text, text_color = f"\u2605{label}", QColor(255, 220, 0)
    else:
        main_text, text_color = "", QColor(255, 255, 255)

    arrow = "\u21e7" if sort_dir == "asc" else "\u21e9"

    if main_text:
        from PySide6.QtCore import QRect as _QRect
        from PySide6.QtGui import QFontMetrics as _QFM
        bar_h = SIZE // 2  # 128px — テキストが確実に入る高さ
        painter.fillRect(0, SIZE - bar_h, SIZE, bar_h, QColor(0, 0, 0, 190))
        # フォントサイズをテキストが収まるよう自動調整（ピクセル単位）
        text_area = _QRect(8, SIZE - bar_h + 8, SIZE - 16, bar_h - 16)
        px = 72  # 開始ピクセルサイズ
        font_main = QFont("Segoe UI", 1, QFont.Weight.Black)
        font_main.setPixelSize(px)
        while px > 8:
            fm = _QFM(font_main)
            br = fm.boundingRect(main_text)
            if br.width() <= text_area.width() and br.height() <= text_area.height():
                break
            px -= 2
            font_main.setPixelSize(px)
        painter.setFont(font_main)
        # 影
        painter.setPen(QColor(0, 0, 0, 230))
        painter.drawText(_QRect(text_area.x() + 2, text_area.y() + 2,
                                text_area.width(), text_area.height()),
                         Qt.AlignmentFlag.AlignCenter, main_text)
        # 本体
        painter.setPen(text_color)
        painter.drawText(text_area, Qt.AlignmentFlag.AlignCenter, main_text)

    # ソート矢印（右下隅）
    from PySide6.QtCore import QRect as _QRect2
    font_arrow = QFont("Segoe UI Symbol", 1, QFont.Weight.Bold)
    font_arrow.setPixelSize(28)
    painter.setFont(font_arrow)
    painter.setPen(QColor(0, 0, 0, 200))
    painter.drawText(_QRect2(SIZE - 50, SIZE - 40, 48, 38),
                     Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, arrow)
    painter.setPen(QColor(255, 255, 255, 230))
    painter.drawText(_QRect2(SIZE - 52, SIZE - 42, 48, 38),
                     Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, arrow)

    painter.end()

    fd, tmp_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        canvas.save(tmp_path)
        with open(tmp_path, "rb") as f:
            png_data = f.read()
    finally:
        os.unlink(tmp_path)
    return "data:image/png;base64," + base64.b64encode(png_data).decode("ascii")


def _make_bplist(title: str, entries: List[MapEntry], image: str = "") -> dict:
    songs = []
    for e in entries:
        char = e.mode or "Standard"
        diff = e.difficulty or "ExpertPlus"
        songs.append({
            "hash": e.song_hash,
            "songName": e.song_name,
            "difficulties": [{"characteristic": char, "name": diff}],
        })
    return {
        "playlistTitle": title,
        "playlistAuthor": "MyBeatSaberStats",
        "image": image,
        "songs": songs,
    }


def _save_bplist(parent: QWidget, title: str, entries: List[MapEntry], init_dir: str = "", image: str = "") -> Optional[str]:
    """bplist ファイルを保存ダイアログで保存する。保存したファイルのパスを返す（キャンセル時は None）。"""
    if not entries:
        QMessageBox.information(parent, "Export", "No maps to export.")
        return None

    safe_title = title.replace(" ", "_").replace("/", "-")
    default_name = str(Path(init_dir) / f"{safe_title}.bplist") if init_dir else f"{safe_title}.bplist"
    path, _ = QFileDialog.getSaveFileName(
        parent, "Save bplist file", default_name,
        "BeatSaber Playlist (*.bplist);;JSON (*.json)"
    )
    if not path:
        return None

    bplist = _make_bplist(title, entries, image)
    try:
        Path(path).write_text(json.dumps(bplist, ensure_ascii=False, indent=2), encoding="utf-8")
        return path
    except Exception as e:
        QMessageBox.critical(parent, "Save Error", str(e))
        return None


def load_accsaber_maps(
    steam_id: Optional[str] = None,
    category: str = "all",
    on_progress=None,
) -> List[MapEntry]:
    """AccSaber のカテゴリプレイリストを API から取得し AccSaber/SS クリア情報を付与する。

    category: "all" | "true" | "standard" | "tech"
    on_progress(done: int, total: int, label: str) — 進捗コールバック（省略可）

    クリア判定の優先順位:
      1. AccSaber player scores API — SC 等の無効モディファイアを除外した公式クリア
      2. SS/BL player scores — AccSaber スコア取得不可時のフォールバック
    """
    _PLAYLIST_URLS: Dict[str, str] = {
        "true":     "https://accsaber.com/api/playlists/true",
        "standard": "https://accsaber.com/api/playlists/standard",
        "tech":     "https://accsaber.com/api/playlists/tech",
    }
    _ACC_DIFF_NORM = {
        "easy": "Easy", "normal": "Normal", "hard": "Hard",
        "expert": "Expert", "expertplus": "ExpertPlus", "expert+": "ExpertPlus",
    }
    cats = ["true", "standard", "tech"] if category == "all" else [category]

    session = requests.Session()

    # AccSaber ranked-maps から (hash.upper(), diff) → complexity インデックスを構築
    complexity_index: Dict[Tuple[str, str], float] = {}
    try:
        rm = session.get("https://accsaber.com/api/ranked-maps", timeout=30)
        if rm.status_code == 200:
            for m in rm.json():
                h = (m.get("songHash") or "").upper()
                dn = _ACC_DIFF_NORM.get((m.get("difficulty") or "").lower(), m.get("difficulty") or "")
                c = m.get("complexity") or 0.0
                if h and dn:
                    complexity_index[(h, dn)] = float(c)
    except Exception:
        pass

    # AccSaber プレイヤースコアを取得し (hash, diff) → cleared/nf セットを構築
    # AccSaber は SC (SmallCubes) 等の特定モディファイアをスコアとしてカウントしないため
    # SS player scores とは独立して AccSaber 公式クリア判定を行う。
    acc_score_cleared: set = set()   # (hash.upper(), diff) — AccSaber 正規クリア
    acc_score_nf: set = set()         # (hash.upper(), diff) — NF クリア
    acc_player_scores_available = False
    if steam_id:
        try:
            ar = session.get(
                f"https://accsaber.com/api/players/{steam_id}/scores?pageSize=2000",
                timeout=15,
            )
            if ar.status_code == 200:
                for asc in ar.json():
                    h = (asc.get("songHash") or "").upper()
                    dn = _ACC_DIFF_NORM.get((asc.get("difficulty") or "").lower(), asc.get("difficulty", ""))
                    mods = (asc.get("mods") or "").upper()
                    if "NF" in mods:
                        acc_score_nf.add((h, dn))
                    else:
                        acc_score_cleared.add((h, dn))
                acc_player_scores_available = True
        except Exception:
            pass

    # SS player scores — pp/acc/rank 表示用、および AccSaber 取得不可時のフォールバック
    ss_scores_raw: Dict[str, dict] = {}
    if steam_id:
        sp = _CACHE_DIR / f"scoresaber_player_scores_{steam_id}.json"
        if sp.exists():
            try:
                sd = json.loads(sp.read_text(encoding="utf-8"))
                ss_scores_raw = sd.get("scores", {})
            except Exception:
                pass
    ss_score_idx = _build_ss_score_hash_index(ss_scores_raw)

    # BL ランクマップキャッシュを読み込んでインデックス化（フォールバック用）
    bl_ranked = load_bl_maps()
    bl_index = _build_bl_hash_index(bl_ranked)

    bl_scores: Dict[str, dict] = {}
    if steam_id:
        bp = _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json"
        if bp.exists():
            try:
                bd = json.loads(bp.read_text(encoding="utf-8"))
                bl_scores = bd.get("scores", {})
            except Exception:
                pass

    from dataclasses import replace as _dc_replace

    # key → (entry, [cat, ...]) で複数カテゴリを集積する
    seen_entries: Dict[Tuple[str, str, str], MapEntry] = {}
    seen_cats: Dict[Tuple[str, str, str], List[str]] = {}

    for i, cat in enumerate(cats):
        if on_progress:
            on_progress(i, len(cats), f"Fetching AccSaber {cat}...")
        url = _PLAYLIST_URLS[cat]
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        bplist_data = resp.json()
        songs = bplist_data.get("songs") or []
        for song in songs:
            s_hash = (song.get("hash") or "").upper()
            s_name = song.get("songName") or ""
            diffs = song.get("difficulties") or []
            for d in diffs:
                char = d.get("characteristic") or "Standard"
                diff_name = _ACC_DIFF_NORM.get((d.get("name") or "").lower(), d.get("name") or "ExpertPlus")
                key = (s_hash, char, diff_name)
                if key not in seen_entries:
                    # SS player scores から pp/acc/rank 取得 (モディファイア有スコアも含む)
                    ss_info = ss_score_idx.get(key)
                    ss_pp = 0.0
                    ss_cleared = ss_nf = False
                    ss_acc = 0.0
                    ss_rank = 0
                    ss_mods = ""
                    if ss_info:
                        ss_pp, ss_cleared, ss_nf, ss_acc, ss_rank, ss_mods = ss_info

                    # BL スコア取得 (フォールバック用)
                    bl_entry = bl_index.get(key)
                    bl_pp = 0.0
                    bl_cleared = bl_nf = False
                    bl_acc_val = 0.0
                    bl_rank = 0
                    bl_stars = 0.0
                    bl_mods = ""
                    if bl_entry:
                        bl_pp, bl_cleared, bl_nf, bl_acc_val, bl_rank, bl_mods = _bl_player_score_info(
                            bl_scores, bl_entry.leaderboard_id
                        )
                        bl_stars = bl_entry.stars

                    # クリア判定: AccSaber 公式スコアを優先
                    key_hd = (s_hash, diff_name)  # AccSaber API にはモード情報なし
                    if acc_player_scores_available:
                        if key_hd in acc_score_cleared:
                            final_cleared, final_nf, final_mods = True, False, ""
                        elif key_hd in acc_score_nf:
                            final_cleared, final_nf, final_mods = False, True, "NF"
                        else:
                            # AccSaber にスコアなし — SS/BL でプレイ済みなら「要再プレイ」扱い
                            if ss_cleared or ss_nf:
                                final_cleared, final_nf, final_mods = False, True, ss_mods
                            elif bl_cleared or bl_nf:
                                final_cleared, final_nf, final_mods = False, True, bl_mods
                            else:
                                final_cleared, final_nf, final_mods = False, False, ""
                    else:
                        # AccSaber スコア取得不可 → SS/BL フォールバック
                        if ss_cleared or ss_nf:
                            final_cleared, final_nf, final_mods = ss_cleared, ss_nf, ss_mods
                        elif bl_cleared or bl_nf:
                            final_cleared, final_nf, final_mods = bl_cleared, bl_nf, bl_mods
                        else:
                            final_cleared, final_nf, final_mods = False, False, ""

                    final_pp = ss_pp or bl_pp
                    final_acc = ss_acc or bl_acc_val
                    final_rank = ss_rank or bl_rank

                    seen_entries[key] = MapEntry(
                        song_name=s_name, song_author="", mapper="",
                        song_hash=s_hash, difficulty=diff_name, mode=char,
                        stars=bl_stars, max_pp=0.0, player_pp=final_pp,
                        cleared=final_cleared, nf_clear=final_nf,
                        player_acc=final_acc, player_rank=final_rank,
                        leaderboard_id="", source="accsaber",
                        acc_category=cat,
                        acc_complexity=complexity_index.get((s_hash, diff_name), 0.0),
                        player_mods=final_mods,
                    )
                    seen_cats[key] = [cat]
                else:
                    seen_cats[key].append(cat)

    # 複数カテゴリに属する場合は "/" で結合
    entries: List[MapEntry] = []
    for key, entry in seen_entries.items():
        cat_list = seen_cats.get(key, [])
        if len(cat_list) > 1:
            entry = _dc_replace(entry, acc_category="/".join(cat_list))
        entries.append(entry)

    if on_progress:
        on_progress(len(cats), len(cats), "Done")
    return entries


def _fetch_rl_ap_index(
    player_id: str,
    session: Optional[requests.Session] = None,
) -> Dict[str, Tuple[float, int]]:
    """AccSaber Reloaded プレイヤーの mapDifficultyId → (ap, rank) インデックスを取得する。"""
    if not player_id:
        return {}
    if session is None:
        session = requests.Session()
    from .accsaber_reloaded import BASE_URL as _RL_BASE, _PAGE_SIZE as _RL_PAGE
    result: Dict[str, Tuple[float, int]] = {}
    page = 0
    while True:
        resp = session.get(
            f"{_RL_BASE}/users/{player_id}/scores",
            params={"page": page, "size": _RL_PAGE},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        for score in data.get("content", []):
            diff_id = score.get("mapDifficultyId")
            ap = float(score.get("ap") or 0)
            rank = int(score.get("rank") or 0)
            if diff_id and ap > 0:
                # 同一難易度に複数スコアがある場合は最大APを保持
                prev_ap = result.get(diff_id, (0.0, 0))[0]
                if prev_ap < ap:
                    result[diff_id] = (ap, rank)
        if data.get("last", True):
            break
        page += 1
    return result


def load_accsaber_reloaded_maps(
    steam_id: Optional[str] = None,
    category: str = "all",
    on_progress=None,
) -> List[MapEntry]:
    """AccSaber Reloaded の全マップを API から取得し BL ランク情報を付与する。

    category: "all" | "true" | "standard" | "tech"
    on_progress(done: int, total: int, label: str) — 進捗コールバック（省略可）
    """
    from .accsaber_reloaded import CATEGORY_IDS

    _RL_DIFF_TO_BS: Dict[str, str] = {
        "EASY":        "Easy",
        "NORMAL":      "Normal",
        "HARD":        "Hard",
        "EXPERT":      "Expert",
        "EXPERT_PLUS": "ExpertPlus",
    }
    _NON_OVERALL_IDS = {k: v for k, v in CATEGORY_IDS.items() if k != "overall"}
    _UUID_TO_CAT: Dict[str, str] = {v: k for k, v in _NON_OVERALL_IDS.items()}  # uuid → "true"/"standard"/"tech"
    target_cat_uuids: set
    if category == "all":
        target_cat_uuids = set(_NON_OVERALL_IDS.values())
    else:
        uuid = _NON_OVERALL_IDS.get(category, "")
        target_cat_uuids = {uuid} if uuid else set()

    # AccSaber Reloaded の全マップを取得
    session = requests.Session()

    def _rl_progress(page: int, total: int) -> None:
        if on_progress:
            on_progress(page, total, f"Fetching AccSaber Reloaded maps... {page}/{total}")

    from .accsaber_reloaded import fetch_all_maps_full
    all_maps = fetch_all_maps_full(session=session, on_progress=_rl_progress)

    # RL プレイヤースコア (AP, rank) を mapDifficultyId でインデックス化
    # mapDifficultyId -> (ap, rank)
    rl_ap_index: Dict[str, Tuple[float, int]] = {}
    if steam_id:
        if on_progress:
            on_progress(0, 1, "Fetching RL player scores (AP)...")
        try:
            rl_ap_index = _fetch_rl_ap_index(steam_id, session=session)
        except Exception:
            pass  # AP 取得失敗時は 0 のまま
        if on_progress:
            on_progress(1, 1, "Done")

    # BL プレイヤースコアを読み込む
    bl_scores: Dict[str, dict] = {}
    if steam_id:
        bp = _CACHE_DIR / f"beatleader_player_scores_{steam_id}.json"
        if bp.exists():
            try:
                bd = json.loads(bp.read_text(encoding="utf-8"))
                bl_scores = bd.get("scores", {})
            except Exception:
                pass

    # SS プレイヤースコアを読み込む（BL スコアが無い場合のフォールバック）
    ss_scores: Dict[str, dict] = {}
    if steam_id:
        sp = _CACHE_DIR / f"scoresaber_player_scores_{steam_id}.json"
        if sp.exists():
            try:
                sd = json.loads(sp.read_text(encoding="utf-8"))
                ss_scores = sd.get("scores", {})
            except Exception:
                pass

    # BL ランクマップキャッシュを hash インデックス化（スター取得用）
    bl_ranked = load_bl_maps()
    bl_index = _build_bl_hash_index(bl_ranked)

    seen: set = set()
    entries: List[MapEntry] = []

    for song in all_maps:
        s_hash = (song.get("songHash") or "").upper()
        s_name = song.get("songName") or ""
        s_author = song.get("songAuthorName") or ""

        for diff in song.get("difficulties") or []:
            if not diff.get("active", False):
                continue
            cat_uuid = diff.get("categoryId", "")
            if cat_uuid not in target_cat_uuids:
                continue
            acc_cat = _UUID_TO_CAT.get(cat_uuid, "")

            char = diff.get("characteristic") or "Standard"
            diff_bs = _RL_DIFF_TO_BS.get(diff.get("difficulty", ""), "ExpertPlus")
            key = (s_hash, char, diff_bs)
            if key in seen:
                continue
            seen.add(key)

            # BL leaderboard ID でプレイヤースコアを取得
            bl_lb_id = str(diff.get("blLeaderboardId") or "")
            complexity = float(diff.get("complexity") or 0.0)
            player_pp, cleared, nf_clear, acc, rank, score_mods = _bl_player_score_info(bl_scores, bl_lb_id)

            # BL 未クリア or NF の場合は SS スコアをフォールバックとして確認
            # (BL が NF でも SS に正規クリアがあれば cleared=True に上書きする)
            if not cleared and ss_scores:
                ss_lb_id = str(diff.get("ssLeaderboardId") or "")
                if ss_lb_id:
                    _, ss_cleared, ss_nf, ss_acc, ss_rank, ss_mods = _ss_player_score_info(ss_scores, ss_lb_id)
                    if ss_cleared or ss_nf:
                        cleared = ss_cleared
                        nf_clear = ss_nf
                        acc = ss_acc or acc
                        rank = ss_rank or rank
                        score_mods = ss_mods

            # RL AP・rank（mapDifficultyId による精定値）
            rl_diff_id = diff.get("id") or ""
            rl_score = rl_ap_index.get(rl_diff_id, (0.0, 0))
            ap = rl_score[0]
            rl_rank = rl_score[1]

            # BL ランクマップからスター取得 (hash+char+diff 一致)
            bl_entry = bl_index.get(key)
            stars = bl_entry.stars if bl_entry else 0.0

            entries.append(MapEntry(
                song_name=s_name,
                song_author=s_author,
                mapper="",
                song_hash=s_hash,
                difficulty=diff_bs,
                mode=char,
                stars=stars,
                max_pp=0.0,
                player_pp=player_pp,
                cleared=cleared,
                nf_clear=nf_clear,
                player_acc=acc,
                player_rank=rl_rank if rl_rank else rank,
                leaderboard_id=bl_lb_id,
                source="accsaber_reloaded",
                acc_category=acc_cat,
                acc_rl_ap=ap,
                acc_complexity=complexity,
                player_mods=score_mods,
            ))

    if on_progress:
        on_progress(1, 1, "Done")
    return entries


# ──────────────────────────────────────────────────────────────────────────────
# スレッド通信用シグナル
# ──────────────────────────────────────────────────────────────────────────────

class _LoadSignals(QObject):
    finished = Signal(list)        # List[MapEntry]
    error = Signal(str)            # エラーメッセージ
    progress = Signal(int, int, str)  # done, total, label


# ──────────────────────────────────────────────────────────────────────────────
# バッチエクスポート
# ──────────────────────────────────────────────────────────────────────────────

_BATCH_SRC_PREFIX: Dict[str, str] = {
    "ss": "SS", "bl": "BL", "acc": "AS", "rl": "RL", "pl": "PL",
}

@dataclass
class _BatchPreset:
    """一括出力プリセットの定義。"""
    label: str
    source: str         # "ss" | "bl" | "rl"
    rl_cat: str         # "true" | "standard" | "tech" | ""
    uncleared: bool     # True = 未クリアのみ
    sort_mode: str      # "star_asc" | "pp_high" | "ap_high"
    filename_base: str  # 出力ファイル名プレフィックス
    split_by_star: bool # True = ★ごとに分割出力


@dataclass
class _BatchConfig:
    """バッチエクスポートの1設定（フィルタ条件として保存）。
    マップデータは保持せず、Export 時に毎回新鮮なデータをロードして適用する。
    """
    label: str
    filename_base: str
    source: str            # "ss" | "bl" | "rl"
    # Status filter
    show_cleared: bool = True
    show_nf: bool = True
    show_unplayed: bool = True
    # Category filter (RL only)
    cat_true: bool = True
    cat_standard: bool = True
    cat_tech: bool = True
    # Star range
    star_min: float = 0.0
    star_max: float = 20.0
    # Export style
    split_mode: str = "star"   # "single" | "star" | "category"
    # Sort
    sort_mode: str = "star_asc"  # "star_asc"|"star_desc"|"pp_high"|"pp_low"|"ap_high"|"ap_low"|"acc_high"|"acc_low"|"rank_low"|"rank_high"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "_BatchConfig":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def display_text(self) -> str:
        src = self.source.upper()
        if self.source == "rl":
            cats = [n for flag, n in [(self.cat_true, "Tr"), (self.cat_standard, "Std"), (self.cat_tech, "Tch")] if flag]
            src = f"RL[{'+'.join(cats) or 'none'}]"
        elif self.source == "acc":
            cats = [n for flag, n in [(self.cat_true, "Tr"), (self.cat_standard, "Std"), (self.cat_tech, "Tch")] if flag]
            src = f"Acc[{'+'.join(cats) or 'none'}]"
        sts = "".join([s for flag, s in [(self.show_cleared, "✓"), (self.show_nf, "⚠"), (self.show_unplayed, "✗")] if flag])
        sort_label = {
            "star_asc": "StarAsc", "star_desc": "StarDesc",
            "pp_high": "PPDesc", "pp_low": "PPAsc",
            "ap_high": "APDesc", "ap_low": "APAsc",
            "acc_high": "AccDesc", "acc_low": "AccAsc",
            "rank_low": "RankAsc", "rank_high": "RankDesc",
        }.get(self.sort_mode, self.sort_mode)
        if self.source in ("rl", "acc"):
            return f"{self.label}  [{src} / {sts} / {self.split_mode} / {sort_label}]"
        star = f"★{self.star_min:g}-{self.star_max:g}"
        return f"{self.label}  [{src} / {sts} / {star} / {self.split_mode} / {sort_label}]"


_BATCH_PRESETS: List[_BatchPreset] = [
    _BatchPreset("SS — Uncleared All",                   "ss", "", True,  "star_asc", "", False),
    _BatchPreset("SS — Uncleared per ★",                 "ss", "", True,  "star_asc", "",    True),
    _BatchPreset("SS — High PP per ★",                   "ss", "", False, "pp_high",  "",    True),
    _BatchPreset("BL — Uncleared All",                   "bl", "", True,  "star_asc", "", False),
    _BatchPreset("BL — Uncleared per ★",                 "bl", "", True,  "star_asc", "",    True),
    _BatchPreset("BL — High PP per ★",                   "bl", "", False, "pp_high",  "",    True),
    _BatchPreset("AccSaber RL — Uncleared per Category", "rl", "", True,  "star_asc", "",    True),
    _BatchPreset("AccSaber RL — High AP per Category",   "rl", "", False, "ap_high",  "",    True),
]


def _apply_config_filter(maps: List[MapEntry], cfg: "_BatchConfig") -> List[MapEntry]:
    """_BatchConfig のフィルタ条件をマップリストに適用してソート済みリストを返す。"""
    result: List[MapEntry] = []
    for e in maps:
        if e.stars < cfg.star_min or e.stars > cfg.star_max:
            continue
        if e.cleared and not cfg.show_cleared:
            continue
        if e.nf_clear and not cfg.show_nf:
            continue
        if not e.played and not cfg.show_unplayed:
            continue
        if cfg.source in ("rl", "acc"):
            if e.acc_category == "true" and not cfg.cat_true:
                continue
            if e.acc_category == "standard" and not cfg.cat_standard:
                continue
            if e.acc_category == "tech" and not cfg.cat_tech:
                continue
        result.append(e)
    if cfg.sort_mode == "pp_high":
        result.sort(key=lambda e: (-e.player_pp, e.stars, e.song_name))
    elif cfg.sort_mode == "pp_low":
        result.sort(key=lambda e: (e.player_pp, e.stars, e.song_name))
    elif cfg.sort_mode == "ap_high":
        result.sort(key=lambda e: (-e.acc_rl_ap, e.stars, e.song_name))
    elif cfg.sort_mode == "ap_low":
        result.sort(key=lambda e: (e.acc_rl_ap, e.stars, e.song_name))
    elif cfg.sort_mode == "acc_high":
        result.sort(key=lambda e: (-e.player_acc, e.stars, e.song_name))
    elif cfg.sort_mode == "acc_low":
        result.sort(key=lambda e: (e.player_acc, e.stars, e.song_name))
    elif cfg.sort_mode == "rank_low":
        result.sort(key=lambda e: (e.player_rank or 999999, e.stars, e.song_name))
    elif cfg.sort_mode == "rank_high":
        result.sort(key=lambda e: (-(e.player_rank or 0), e.stars, e.song_name))
    elif cfg.sort_mode == "star_desc":
        result.sort(key=lambda e: (-e.stars, e.song_name))
    else:
        result.sort(key=lambda e: (e.stars, e.song_name))
    return result


def _pregenerate_covers(configs: "List[_BatchConfig]") -> Dict[str, str]:
    """必要なカバー画像を事前生成してキャッシュ辞書を返す（メインスレッドで呼ぶこと）。"""
    cache: Dict[str, str] = {}
    for cfg in configs:
        sd = _sort_dir_from_mode(cfg.sort_mode)
        if cfg.split_mode == "star":
            for si in range(21):
                key = f"star:{si}:{sd}:{cfg.source}"
                if key not in cache:
                    cache[key] = _make_playlist_cover("star", str(si), sd, cfg.source)
        elif cfg.split_mode == "category":
            for cat in ("true", "standard", "tech", "unknown"):
                key = f"cat:{cat}:{sd}:{cfg.source}"
                if key not in cache:
                    cache[key] = _make_playlist_cover(cat, "", sd, cfg.source)
        else:
            if cfg.source in ("rl", "acc"):
                # RL/Acc single: cat フラグが1つだけ True ならそのカテゴリテキストを使用
                _rl_cats = [c for c, f in [("true", cfg.cat_true), ("standard", cfg.cat_standard), ("tech", cfg.cat_tech)] if f]
                _rl_ct = _rl_cats[0] if len(_rl_cats) == 1 else "default"
                key = f"acc_single:{cfg.source}:{_rl_ct}:{sd}"
                if key not in cache:
                    cache[key] = _make_playlist_cover(_rl_ct, "", sd, cfg.source)
            else:
                key = f"default:{sd}:{cfg.source}"
                if key not in cache:
                    cache[key] = _make_playlist_cover("default", "", sd, cfg.source)
    return cache


def _config_export_tag(cfg: "_BatchConfig") -> str:
    """_BatchConfig のフィルタ・ソート条件からファイル名タグを生成する。"""
    parts: List[str] = []
    sts = []
    if cfg.show_cleared:
        sts.append("Cleared")
    if cfg.show_nf:
        sts.append("NF")
    if cfg.show_unplayed:
        sts.append("Unplayed")
    if len(sts) < 3:
        parts.append("+".join(sts) if sts else "none")
    if cfg.star_min > 0.0 or cfg.star_max < 20.0:
        parts.append(f"star{cfg.star_min:g}-{cfg.star_max:g}")
    if cfg.source in ("rl", "acc"):
        cats = [n for flag, n in [(cfg.cat_true, "T"), (cfg.cat_standard, "S"), (cfg.cat_tech, "Tc")] if flag]
        if len(cats) < 3:
            parts.append("+".join(cats) if cats else "nocat")
    _sort_tags = {
        "star_asc": "StarAsc", "star_desc": "StarDesc",
        "pp_high": "PPDesc", "pp_low": "PPAsc",
        "ap_high": "APDesc", "ap_low": "APAsc",
        "acc_high": "AccDesc", "acc_low": "AccAsc",
        "rank_low": "RankAsc", "rank_high": "RankDesc",
    }
    parts.append(_sort_tags.get(cfg.sort_mode, cfg.sort_mode))
    return "_".join(parts)


_SORT_SYMBOL: Dict[str, str] = {
    "star_asc":  "★↑",
    "star_desc": "★↓",
    "pp_high":   "PP↓",
    "pp_low":    "PP↑",
    "ap_high":   "AP↓",
    "ap_low":    "AP↑",
    "acc_high":  "Acc↓",
    "acc_low":   "Acc↑",
    "rank_low":  "Rank↑",
    "rank_high": "Rank↓",
}
_CAT_LABEL: Dict[str, str] = {
    "true": "True", "standard": "Standard", "tech": "Tech",
}
_SRC_LABEL: Dict[str, str] = {
    "ss": "SS", "bl": "BL", "rl": "RL", "acc": "Acc",
}


def _playlist_title(
    cfg: "_BatchConfig",
    star_group: Optional[int] = None,
    category: Optional[str] = None,
) -> str:
    """プレイリストタイトルを生成する。
    形式: {サービス}★{番号} / {サービス} {カテゴリ}  +  {フィルター(あれば)}  +  {ソート記号}
    """
    src = _SRC_LABEL.get(cfg.source, cfg.source.upper())
    sort_sym = _SORT_SYMBOL.get(cfg.sort_mode, cfg.sort_mode)

    # --- カテゴリ / ★ 部分 ---
    if star_group is not None:
        head = f"{src}★{star_group}"
    elif category is not None:
        head = f"{src} {_CAT_LABEL.get(category, category.capitalize())}"
    else:  # single
        if cfg.source in ("rl", "acc"):
            rl_cats = [c for c, f in [("true", cfg.cat_true), ("standard", cfg.cat_standard), ("tech", cfg.cat_tech)] if f]
            if len(rl_cats) == 1:
                head = f"{src} {_CAT_LABEL.get(rl_cats[0], rl_cats[0].capitalize())}"
            else:
                head = src
        else:
            head = src

    # --- フィルター部分（全ステータスが有効な場合は省略）---
    filter_parts: List[str] = []
    if not (cfg.show_cleared and cfg.show_nf and cfg.show_unplayed):
        if cfg.show_nf:
            filter_parts.append("NF")
        if cfg.show_unplayed:
            filter_parts.append("Unplayed")
        if cfg.show_cleared:
            filter_parts.append("Cleared")
    filter_str = "+".join(filter_parts)

    parts = [p for p in [head, filter_str, sort_sym] if p]
    return " ".join(parts)


def _write_config_files(
    maps: List[MapEntry],
    cfg: "_BatchConfig",
    folder_path: Path,
    saved: List[str],
    errors: List[str],
    covers: Dict[str, str],
) -> None:
    """_BatchConfig の split_mode に従ってファイルを書き出す。"""
    tag = _config_export_tag(cfg)
    src_pfx = _BATCH_SRC_PREFIX.get(cfg.source, cfg.source.upper())
    _legacy = {"All", "single", "split", "cat"}
    fname_base = "" if cfg.filename_base in _legacy else cfg.filename_base
    fbase = "_".join(p for p in [src_pfx, fname_base, tag] if p)
    if cfg.split_mode == "star":
        groups: Dict[int, List[MapEntry]] = {}
        for e in maps:
            si = max(1, math.floor(e.stars)) if e.stars > 0 else 0
            groups.setdefault(si, []).append(e)
        _sort_dir = _sort_dir_from_mode(cfg.sort_mode)
        for si in sorted(groups.keys()):
            fname = f"{fbase}_{si:02d}star.bplist"
            _img = covers.get(f"star:{si}:{_sort_dir}:{cfg.source}", "")
            bplist = _make_bplist(_playlist_title(cfg, star_group=si), groups[si], _img)
            (folder_path / fname).write_text(
                json.dumps(bplist, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved.append(fname)
    elif cfg.split_mode == "category":
        cat_groups: Dict[str, List[MapEntry]] = {"true": [], "standard": [], "tech": []}
        for e in maps:
            cat = e.acc_category or "unknown"
            cat_groups.setdefault(cat, []).append(e)
        _sort_dir = _sort_dir_from_mode(cfg.sort_mode)
        for cat in sorted(cat_groups.keys()):
            fname = f"{fbase}_{cat.capitalize()}.bplist"
            _img = covers.get(f"cat:{cat}:{_sort_dir}:{cfg.source}", "")
            bplist = _make_bplist(_playlist_title(cfg, category=cat), cat_groups[cat], _img)
            (folder_path / fname).write_text(
                json.dumps(bplist, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            saved.append(fname)
    else:  # "single"
        fname = f"{fbase}.bplist"
        _sort_dir = _sort_dir_from_mode(cfg.sort_mode)
        if cfg.source in ("rl", "acc"):
            _rl_cats = [c for c, f in [("true", cfg.cat_true), ("standard", cfg.cat_standard), ("tech", cfg.cat_tech)] if f]
            _rl_ct = _rl_cats[0] if len(_rl_cats) == 1 else "default"
            _img = covers.get(f"acc_single:{cfg.source}:{_rl_ct}:{_sort_dir}", "")
        else:
            _img = covers.get(f"default:{_sort_dir}:{cfg.source}", "")
        bplist = _make_bplist(_playlist_title(cfg), maps, _img)
        (folder_path / fname).write_text(
            json.dumps(bplist, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        saved.append(fname)


def _run_export_configs(
    sigs: "_LoadSignals",
    steam_id: Optional[str],
    configs: "List[_BatchConfig]",
    folder_path: Path,
    covers: Dict[str, str],
) -> None:
    """バッチ設定リストを使って最新データをロードしてエクスポートする（スレッド実行）。
    完了時: sigs.finished.emit([saved_files, errors])
    """
    has_ss = any(c.source == "ss" for c in configs)
    has_bl = any(c.source == "bl" for c in configs)
    has_rl = any(c.source == "rl" for c in configs)
    has_acc = any(c.source == "acc" for c in configs)

    ss_maps: List[MapEntry] = []
    bl_maps: List[MapEntry] = []
    rl_maps: List[MapEntry] = []
    acc_maps: List[MapEntry] = []

    n_load = (1 if has_ss else 0) + (1 if has_bl else 0) + (1 if has_rl else 0) + (1 if has_acc else 0)
    total = n_load + len(configs)
    step = 0

    try:
        if has_ss:
            sigs.progress.emit(step, total, "Loading SS ranked maps...")
            ss_maps = load_ss_maps(steam_id)
            step += 1
        if has_bl:
            sigs.progress.emit(step, total, "Loading BL ranked maps...")
            bl_maps = load_bl_maps(steam_id)
            step += 1
        if has_rl:
            _rl_step = step

            def _rl_prog(d: int, t: int, label: str) -> None:
                sigs.progress.emit(_rl_step, total, label)

            sigs.progress.emit(step, total, "Fetching AccSaber RL maps...")
            rl_maps = load_accsaber_reloaded_maps(steam_id, "all", on_progress=_rl_prog)
            step += 1
        if has_acc:
            _acc_step = step

            def _acc_prog(d: int, t: int, label: str) -> None:
                sigs.progress.emit(_acc_step, total, label)

            sigs.progress.emit(step, total, "Fetching AccSaber maps...")
            acc_maps = load_accsaber_maps(steam_id, "all", on_progress=_acc_prog)
            step += 1

        saved_files: List[str] = []
        errors: List[str] = []

        for cfg in configs:
            sigs.progress.emit(step, total, f"Exporting: {cfg.label}...")
            try:
                base = {"ss": ss_maps, "bl": bl_maps, "rl": rl_maps, "acc": acc_maps}.get(cfg.source, [])
                maps = _apply_config_filter(list(base), cfg)
                _write_config_files(maps, cfg, folder_path, saved_files, errors, covers)
            except Exception as exc:
                errors.append(f"{cfg.label}: {exc}")
            step += 1

        sigs.finished.emit([saved_files, errors])
    except Exception as top_exc:
        sigs.error.emit(str(top_exc))


# ──────────────────────────────────────────────────────────────────────────────
# PlaylistWindow
# ──────────────────────────────────────────────────────────────────────────────

# テーブル列インデックス
_COL_STATUS = 0
_COL_SONG = 1
_COL_DIFF = 2
_COL_MODE = 3
_COL_STARS = 4
_COL_PLAYER_PP = 5
_COL_ACC_CAT = 6
_COL_RL_AP = 7
_COL_PLAYER_ACC = 8
_COL_PLAYER_RANK = 9
_COL_MOD = 10
_COL_MAPPER = 11
_COL_AUTHOR = 12
_COL_COUNT = 13

_COL_LABELS = [
    "Status", "Song", "Diff", "Mode", "★", "Player PP", "Acc Category", "AP", "Acc %", "Rank",
    "Mods", "Mapper", "Author",
]


class _PresetListWidget(QListWidget):
    """行テキストクリックでもチェックボックスをトグルできる QListWidget。"""

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        item = self.itemAt(event.pos())
        if item is not None and event.button() == Qt.MouseButton.LeftButton:
            before = item.checkState()
            super().mouseReleaseEvent(event)
            # Qt が (already-selected 等の理由で) トグルしなかった場合は手動トグル
            if item.checkState() == before:
                new = (
                    Qt.CheckState.Unchecked
                    if before == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
                item.setCheckState(new)
        else:
            super().mouseReleaseEvent(event)


class PlaylistWindow(QMainWindow):
    """Playlist 画面デモ。"""

    def __init__(
        self,
        steam_id: Optional[str] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Playlist")
        self.resize(1400, 750)

        self._steam_id = steam_id
        self._all_entries: List[MapEntry] = []   # ロード済み全データ
        self._filtered: List[MapEntry] = []       # フィルタ後データ
        self._load_signals = _LoadSignals()
        self._load_signals.finished.connect(self._on_load_finished)
        self._load_signals.error.connect(self._on_load_error)
        self._load_signals.progress.connect(self._on_load_progress)
        self._progress_dlg: Optional[QProgressDialog] = None

        central = QWidget(self)
        self.setCentralWidget(central)
        _main_layout = QHBoxLayout(central)
        _main_layout.setSpacing(0)
        _main_layout.setContentsMargins(4, 4, 4, 4)
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.setChildrenCollapsible(False)
        _main_layout.addWidget(self._splitter)
        self.__cols = self._splitter  # right panel を後で追加するため保持
        _left_w = QWidget()
        root = QVBoxLayout(_left_w)
        root.setSpacing(4)
        root.setContentsMargins(2, 2, 2, 2)
        self._splitter.addWidget(_left_w)

        # ─ Source ───────────────────────────────────────────────────
        src_group = QGroupBox("Source")
        src_vbox = QVBoxLayout(src_group)
        src_vbox.setSpacing(4)
        src_vbox.setContentsMargins(6, 16, 6, 4)

        self._src_group = QButtonGroup(self)
        self._rb_ss = QRadioButton(SOURCE_SS)
        self._rb_bl = QRadioButton(SOURCE_BL)
        self._rb_acc = QRadioButton(SOURCE_ACC)
        self._rb_acc_rl = QRadioButton(SOURCE_ACC_RL)
        self._rb_open = QRadioButton(SOURCE_OPEN)
        self._rb_ss.setChecked(True)

        # 1行目: ScoreSaber / BeatLeader / AccSaber / AccSaber RL
        src_row1 = QHBoxLayout()
        src_row1.setSpacing(8)
        for i, rb in enumerate([self._rb_ss, self._rb_bl, self._rb_acc, self._rb_acc_rl]):
            self._src_group.addButton(rb, i)
            src_row1.addWidget(rb)
        src_row1.addStretch()
        src_vbox.addLayout(src_row1)

        # 2行目: Open File + ファイル操作 + Load ボタン
        src_row2 = QHBoxLayout()
        src_row2.setSpacing(8)
        self._src_group.addButton(self._rb_open, 4)
        src_row2.addWidget(self._rb_open)

        # Open file
        self._open_edit = QLineEdit()
        self._open_edit.setPlaceholderText(".bplist / .json file path...")
        self._open_edit.setEnabled(False)
        self._open_edit.setMinimumWidth(240)
        self._btn_browse = QPushButton("Browse...")
        self._btn_browse.setEnabled(False)
        self._btn_browse.clicked.connect(self._browse_bplist)
        self._svc_label = QLabel("Service:")
        self._svc_label.setEnabled(False)
        self._svc_combo = QComboBox()
        self._svc_combo.addItem("None", userData="none")
        self._svc_combo.addItem("ScoreSaber", userData="scoresaber")
        self._svc_combo.addItem("BeatLeader", userData="beatleader")
        self._svc_combo.addItem("AccSaber", userData="accsaber")
        self._svc_combo.addItem("AccSaber RL", userData="accsaber_rl")
        self._svc_combo.setEnabled(False)
        self._svc_combo.currentIndexChanged.connect(self._on_svc_combo_changed)
        src_row2.addWidget(self._open_edit)
        src_row2.addWidget(self._btn_browse)
        src_row2.addWidget(self._svc_label)
        src_row2.addWidget(self._svc_combo)
        src_row2.addStretch()

        self._btn_load = QPushButton("⏵  Load")
        self._btn_load.setMinimumHeight(28)
        self._btn_load.setMinimumWidth(90)
        self._btn_load.setStyleSheet(
            "QPushButton { background-color: #1976D2; color: white; font-weight: bold;"
            " border-radius: 4px; padding: 2px 10px; }"
            " QPushButton:hover { background-color: #1E88E5; }"
            " QPushButton:pressed { background-color: #1565C0; }"
            " QPushButton:disabled { background-color: #555; color: #aaa; }"
        )
        self._btn_load.clicked.connect(self._load_data)
        src_row2.addWidget(self._btn_load)
        src_vbox.addLayout(src_row2)

        self._src_group.buttonToggled.connect(self._on_source_changed)
        root.addWidget(src_group)

        # ─ Filter ───────────────────────────────────────────────────
        filter_group = QGroupBox("Filter")
        filter_row = QHBoxLayout(filter_group)
        filter_row.setSpacing(8)

        filter_row.addWidget(QLabel("🔍 Song:"))
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Search by song / author / mapper...")
        self._search_edit.setMinimumWidth(180)
        self._search_edit.textChanged.connect(self._apply_filter)
        self._search_edit.textChanged.connect(
            lambda text: self._btn_add_to_batch.setVisible(not text.strip())
        )
        filter_row.addWidget(self._search_edit)

        self._star_label = QLabel("★")
        filter_row.addWidget(self._star_label)
        self._star_min = QDoubleSpinBox()
        self._star_min.setRange(0.0, 20.0)
        self._star_min.setDecimals(1)
        self._star_min.setSingleStep(0.5)
        self._star_min.setValue(0.0)
        self._star_min.setFixedWidth(68)
        self._star_min.valueChanged.connect(self._apply_filter)
        filter_row.addWidget(self._star_min)
        self._star_sep_label = QLabel("–")
        filter_row.addWidget(self._star_sep_label)
        self._star_max = QDoubleSpinBox()
        self._star_max.setRange(0.0, 20.0)
        self._star_max.setDecimals(1)
        self._star_max.setSingleStep(0.5)
        self._star_max.setValue(20.0)
        self._star_max.setFixedWidth(68)
        self._star_max.valueChanged.connect(self._apply_filter)
        filter_row.addWidget(self._star_max)

        filter_row.addSpacing(8)
        filter_row.addWidget(QLabel("Status:"))
        self._cb_sts_cleared = QCheckBox("Cleared ✔")
        self._cb_sts_cleared.setChecked(True)
        self._cb_sts_cleared.toggled.connect(self._apply_filter)
        self._cb_sts_nf = QCheckBox("NF ⚠")
        self._cb_sts_nf.setChecked(True)
        self._cb_sts_nf.toggled.connect(self._apply_filter)
        self._cb_sts_unplayed = QCheckBox("Unplayed ✖")
        self._cb_sts_unplayed.setChecked(True)
        self._cb_sts_unplayed.toggled.connect(self._apply_filter)
        filter_row.addWidget(self._cb_sts_cleared)
        filter_row.addWidget(self._cb_sts_nf)
        filter_row.addWidget(self._cb_sts_unplayed)

        filter_row.addSpacing(8)
        self._cat_filter_label = QLabel("Category:")
        self._cat_filter_label.setVisible(False)
        filter_row.addWidget(self._cat_filter_label)
        self._cb_cat_true = QCheckBox("True")
        self._cb_cat_true.setChecked(True)
        self._cb_cat_true.setVisible(False)
        self._cb_cat_true.toggled.connect(self._apply_filter)
        self._cb_cat_standard = QCheckBox("Standard")
        self._cb_cat_standard.setChecked(True)
        self._cb_cat_standard.setVisible(False)
        self._cb_cat_standard.toggled.connect(self._apply_filter)
        self._cb_cat_tech = QCheckBox("Tech")
        self._cb_cat_tech.setChecked(True)
        self._cb_cat_tech.setVisible(False)
        self._cb_cat_tech.toggled.connect(self._apply_filter)
        filter_row.addWidget(self._cb_cat_true)
        filter_row.addWidget(self._cb_cat_standard)
        filter_row.addWidget(self._cb_cat_tech)

        filter_row.addStretch()
        self._count_label = QLabel("0 maps")
        filter_row.addWidget(self._count_label)

        root.addWidget(filter_group)

        # ─ Export ───────────────────────────────────────────────────
        export_group = QGroupBox("Export")
        export_row = QHBoxLayout(export_group)
        export_row.setSpacing(12)

        # Split 条件
        export_row.addWidget(QLabel("Style:"))
        self._export_style_grp = QButtonGroup(self)
        self._rb_exp_single = QRadioButton("Single file")
        self._rb_exp_single.setToolTip("単一ファイルとして出力します")

        self._rb_exp_split = QRadioButton("Split by ★")
        self._rb_exp_split.setToolTip("★ごとにファイルを分割して出力します")

        self._rb_exp_single.setChecked(True)
        self._export_style_grp.addButton(self._rb_exp_single, 0)
        self._export_style_grp.addButton(self._rb_exp_split, 1)
        export_row.addWidget(self._rb_exp_single)
        export_row.addWidget(self._rb_exp_split)

        export_row.addSpacing(16)
        export_row.addWidget(QLabel("Sort:"))
        self._sort_label = QLabel("★ ↑")
        self._sort_label.setStyleSheet("color: #aaa; font-style: italic;")
        self._sort_label.setToolTip("テーブルヘッダをクリックしてソートを変えるとここに反映されます")
        export_row.addWidget(self._sort_label)

        export_row.addStretch()

        self._btn_export = QPushButton("📤 Export")
        self._btn_export.setStyleSheet(
            "QPushButton { font-weight: bold; padding: 2px 8px; }"
        )
        self._btn_export.setToolTip(
            "Content と Style の条件に従って bplist を出力します。\n"
            "フィルタ中の範囲が対象です。\n"
            "Split by ★ の場合は保存フォルダを選択してください。"
        )
        self._btn_export.clicked.connect(self._on_export)
        export_row.addWidget(self._btn_export)

        self._btn_add_to_batch = QPushButton("➕ Add to Batch")
        self._btn_add_to_batch.setToolTip(
            "フィルタ中のマップを Batch Export キューに追加します。\n"
            "Content / Style の設定が反映されます。"
        )
        self._btn_add_to_batch.clicked.connect(self._add_to_batch)
        export_row.addWidget(self._btn_add_to_batch)

        self._export_info_label = QLabel("")
        export_row.addWidget(self._export_info_label)

        root.addWidget(export_group)

        # ─ テーブル ─────────────────────────────────────────────────
        self._table = QTableWidget(0, _COL_COUNT, self)
        self._table.setHorizontalHeaderLabels(_COL_LABELS)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSortingEnabled(True)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(table_stylesheet())
        self._table.verticalHeader().setDefaultSectionSize(18)
        self._table.verticalHeader().setVisible(False)

        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        hdr.setSectionsMovable(True)
        hdr.sectionClicked.connect(self._on_header_clicked)

        # 初期列幅調整
        self._table.setColumnWidth(_COL_STATUS, 52)
        self._table.setColumnWidth(_COL_SONG, 240)
        self._table.setColumnWidth(_COL_DIFF, 26)
        self._table.setColumnWidth(_COL_MODE, 42)
        self._table.setColumnWidth(_COL_STARS, 64)
        self._table.setColumnWidth(_COL_PLAYER_PP, 72)
        self._table.setColumnWidth(_COL_RL_AP, 72)
        self._table.setColumnWidth(_COL_PLAYER_ACC, 64)
        self._table.setColumnWidth(_COL_PLAYER_RANK, 52)
        self._table.setColumnWidth(_COL_MOD, 68)
        self._table.setColumnWidth(_COL_AUTHOR, 140)
        self._table.setColumnWidth(_COL_MAPPER, 120)
        self._table.setColumnWidth(_COL_ACC_CAT, 90)

        root.addWidget(self._table, 1)

        # ─ Right panel: Batch Export ────────────────────────────────────
        _right_w = QWidget()
        _right_w.setMinimumWidth(180)
        _rl = QVBoxLayout(_right_w)
        _rl.setSpacing(4)
        _rl.setContentsMargins(4, 4, 4, 4)
        self.__cols.addWidget(_right_w)

        # 右パネル内を縦スプリッタで分割 (上: Batch Queue / 下: Quick Presets)
        _right_splitter = QSplitter(Qt.Orientation.Vertical)
        _right_splitter.setChildrenCollapsible(False)
        _rl.addWidget(_right_splitter)

        # ── 上ペイン: Batch Export ──
        _top_pane = QWidget()
        _top_layout = QVBoxLayout(_top_pane)
        _top_layout.setSpacing(6)
        _top_layout.setContentsMargins(0, 0, 0, 0)
        _right_splitter.addWidget(_top_pane)

        _batch_title_row = QHBoxLayout()
        _batch_title = QLabel("Batch Export")
        _batch_title.setStyleSheet("font-weight: bold; font-size: 13px;")
        _batch_title_row.addWidget(_batch_title)
        _batch_title_row.addStretch()
        self._btn_preview_cover = QPushButton("🖼️ Playlist Covers")
        self._btn_preview_cover.setToolTip("出力フォルダを選んで .bplist のカバー画像を一覧表示します")
        self._btn_preview_cover.clicked.connect(self._show_cover_preview)
        _batch_title_row.addWidget(self._btn_preview_cover)
        _top_layout.addLayout(_batch_title_row)

        self._batch_queue_list = QListWidget()
        self._batch_queue_list.setAlternatingRowColors(True)
        self._batch_queue_list.setWordWrap(True)
        self._batch_queue_list.setToolTip("一括出力待ちのプレイリスト一覧")
        _top_layout.addWidget(self._batch_queue_list, 1)

        _queue_btn_row = QHBoxLayout()
        self._batch_count_label = QLabel("0 items")
        _queue_btn_row.addWidget(self._batch_count_label)
        _queue_btn_row.addStretch()
        _btn_bq_remove = QPushButton("Remove")
        _btn_bq_remove.setFixedWidth(62)
        _btn_bq_remove.clicked.connect(self._batch_remove_selected)
        _queue_btn_row.addWidget(_btn_bq_remove)
        _btn_bq_clear = QPushButton("Clear")
        _btn_bq_clear.setFixedWidth(46)
        _btn_bq_clear.clicked.connect(self._batch_clear)
        _queue_btn_row.addWidget(_btn_bq_clear)
        _top_layout.addLayout(_queue_btn_row)

        _export_all_btn_row = QHBoxLayout()
        self._btn_batch_export_all = QPushButton("📤 Export All")
        self._btn_batch_export_all.clicked.connect(self._batch_export_all)
        self._btn_batch_export_all.setFixedHeight(26)
        self._btn_batch_export_all.setStyleSheet(
            "QPushButton { background-color: #1a6b3a; color: white; font-weight: bold; font-size: 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #1e8046; }"
            "QPushButton:disabled { background-color: #444; color: #888; }"
        )
        _export_all_btn_row.addWidget(self._btn_batch_export_all)
        _top_layout.addLayout(_export_all_btn_row)

        # ── 下ペイン: Quick Presets ──
        _bot_pane = QWidget()
        _bot_layout = QVBoxLayout(_bot_pane)
        _bot_layout.setSpacing(6)
        _bot_layout.setContentsMargins(0, 0, 0, 0)
        _right_splitter.addWidget(_bot_pane)

        _bot_layout.addWidget(QLabel("Quick Presets:"))

        self._preset_list_w = _PresetListWidget()
        self._preset_list_w.setAlternatingRowColors(True)
        for _p in _BATCH_PRESETS:
            _pi = QListWidgetItem(_p.label)
            _pi.setFlags(_pi.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            _pi.setCheckState(Qt.CheckState.Unchecked)
            _pi.setData(Qt.ItemDataRole.UserRole, _p)
            self._preset_list_w.addItem(_pi)
        _bot_layout.addWidget(self._preset_list_w, 1)

        _preset_btn_row = QHBoxLayout()
        _btn_pa = QPushButton("All")
        _btn_pa.setFixedWidth(36)
        _btn_pa.clicked.connect(
            lambda: [self._preset_list_w.item(i).setCheckState(Qt.CheckState.Checked)
                     for i in range(self._preset_list_w.count())]
        )
        _btn_pn = QPushButton("None")
        _btn_pn.setFixedWidth(44)
        _btn_pn.clicked.connect(
            lambda: [self._preset_list_w.item(i).setCheckState(Qt.CheckState.Unchecked)
                     for i in range(self._preset_list_w.count())]
        )
        _preset_btn_row.addWidget(_btn_pa)
        _preset_btn_row.addWidget(_btn_pn)
        _preset_btn_row.addStretch()
        self._btn_add_presets = QPushButton("➕ Add to Batch")
        self._btn_add_presets.clicked.connect(self._batch_add_presets)
        _preset_btn_row.addWidget(self._btn_add_presets)
        _bot_layout.addLayout(_preset_btn_row)
        self._btn_quick_export = QPushButton("📤 Quick Export")
        self._btn_quick_export.clicked.connect(self._quick_export_presets)
        self._btn_quick_export.setFixedHeight(26)
        self._btn_quick_export.setStyleSheet(
            "QPushButton { background-color: #1a4a6b; color: white; font-weight: bold; font-size: 12px; border-radius: 4px; }"
            "QPushButton:hover { background-color: #1e5a80; }"
            "QPushButton:disabled { background-color: #444; color: #888; }"
        )
        _bot_layout.addWidget(self._btn_quick_export)

        _right_splitter.setSizes([300, 200])

        # ── バッチ状態 ──
        self._export_dir: str = self._load_export_dir()
        self._batch_configs: List[_BatchConfig] = self._batch_load_configs()
        self._export_sigs = _LoadSignals()
        self._export_sigs.finished.connect(self._on_export_finished)
        self._export_sigs.error.connect(self._on_export_error)
        self._export_sigs.progress.connect(self._on_export_progress)
        self._batch_progress_dlg: Optional[QProgressDialog] = None
        self._batch_refresh_queue()

        # スプリッタ初期サイズ: 左を広く、右パネルを 252px
        self._splitter.setSizes([980, 420])
        self._load_window_state()

    def _save_window_state(self) -> None:
        try:
            payload: dict = {}
            if _PLAYLIST_WINDOW_PATH.exists():
                try:
                    payload = json.loads(_PLAYLIST_WINDOW_PATH.read_text(encoding="utf-8"))
                except Exception:
                    payload = {}
            payload["splitter_sizes"] = self._splitter.sizes()
            payload["window_width"] = self.width()
            payload["window_height"] = self.height()
            _PLAYLIST_WINDOW_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _load_window_state(self) -> None:
        if not _PLAYLIST_WINDOW_PATH.exists():
            return
        try:
            data = json.loads(_PLAYLIST_WINDOW_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        w = data.get("window_width")
        h = data.get("window_height")
        if isinstance(w, int) and isinstance(h, int) and w > 200 and h > 200:
            self.resize(w, h)
        sizes = data.get("splitter_sizes")
        if isinstance(sizes, list) and len(sizes) == 2:
            self._splitter.setSizes(sizes)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._save_window_state()
        super().closeEvent(event)

    def apply_theme(self) -> None:
        """テーマ切替後に呼び出してテーブルスタイルと行色を更新する。"""
        self._table.setStyleSheet(table_stylesheet())
        if self._all_entries:
            self._refresh_table(self._filtered)

    # ──────────────────────────────────────────────────────────────────────────
    # ソース選択イベント
    # ──────────────────────────────────────────────────────────────────────────

    def _on_source_changed(self, btn, checked: bool) -> None:
        open_mode = self._rb_open.isChecked()
        self._open_edit.setEnabled(open_mode)
        self._btn_browse.setEnabled(open_mode)
        self._svc_label.setEnabled(open_mode)
        self._svc_combo.setEnabled(open_mode)
        # Open モードでは Batch Export に追加できないので非表示
        self._btn_add_to_batch.setVisible(not open_mode)
        # open 以外のソースに切り替えたらヘッダを元に戻す
        if not open_mode:
            hdr_item = self._table.horizontalHeaderItem(_COL_ACC_CAT)
            if hdr_item is not None:
                hdr_item.setText("Acc Category")
        # Filter / Export UI をソース状態に合わせて更新
        self._update_filter_export_ui()
        # ソースに応じて PP / Acc / Rank 列ヘッダを切り替え
        self._update_score_headers()

    def _update_filter_export_ui(self) -> None:
        """現在のソース/サービス設定に応じて Filter・Export の表示状態を更新する。"""
        is_acc = self._rb_acc.isChecked() or self._rb_acc_rl.isChecked() or (
            self._rb_open.isChecked() and self._svc_combo.currentData() == "accsaber_rl"
        )
        # AccSaber / AccSaber RL ではカテゴリ別分割なので Split ラベルを切り替え
        self._rb_exp_split.setText("Split by Category (True / Standard / Tech)" if is_acc else "Split by ★")
        # Category filter チェックボックスは AccSaber / AccSaber RL のときのみ表示
        for w in [self._cat_filter_label, self._cb_cat_true, self._cb_cat_standard, self._cb_cat_tech]:
            w.setVisible(is_acc)
        # ★レンジは AccSaber / AccSaber RL では非表示
        for w in [self._star_label, self._star_min, self._star_sep_label, self._star_max]:
            w.setVisible(not is_acc)

    def _on_svc_combo_changed(self) -> None:
        """サービスコンボ変更時に Filter/Export UI とテーブルヘッダを更新する。"""
        self._update_filter_export_ui()
        self._update_score_headers()

    def _update_score_headers(self) -> None:
        """ソースに応じて PP / Acc % / Rank 列のヘッダを切り替える。"""
        if self._rb_ss.isChecked():
            pp_label, acc_label, rank_label = "SS PP", "Acc %", "SS Rank"
        elif self._rb_bl.isChecked():
            pp_label, acc_label, rank_label = "BL PP", "Acc %", "BL Rank"
        elif self._rb_acc.isChecked():
            # AccSaber は SS スコアを基本とし BL をフォールバック
            pp_label, acc_label, rank_label = "SS PP", "Acc %", "SS Rank"
        elif self._rb_acc_rl.isChecked():
            # RL は BL/SS PP を使用・Rank は AccSaber RL のマップ内順位
            pp_label, acc_label, rank_label = "PP", "Acc %", "RL Rank"
        else:
            # open モード: service コンボで決まる
            svc = self._svc_combo.currentData() or "none"
            if svc == "scoresaber":
                pp_label, acc_label, rank_label = "SS PP", "Acc %", "SS Rank"
            elif svc == "beatleader":
                pp_label, acc_label, rank_label = "BL PP", "Acc %", "BL Rank"
            elif svc == "accsaber_rl":
                pp_label, acc_label, rank_label = "PP", "Acc %", "RL Rank"
            else:
                pp_label, acc_label, rank_label = "Player PP", "Acc %", "Rank"
        for col, label in [
            (_COL_PLAYER_PP, pp_label),
            (_COL_PLAYER_ACC, acc_label),
            (_COL_PLAYER_RANK, rank_label),
        ]:
            item = self._table.horizontalHeaderItem(col)
            if item is not None:
                item.setText(label)
        # AccSaber / AccSaber RL 時は★列を Category に切り替え
        is_acc_mode = self._rb_acc.isChecked() or self._rb_acc_rl.isChecked() or (
            self._rb_open.isChecked() and self._svc_combo.currentData() == "accsaber_rl"
        )
        star_hdr = self._table.horizontalHeaderItem(_COL_STARS)
        if star_hdr is not None:
            star_hdr.setText("Cmplx" if is_acc_mode else "★")

    def _current_sort_mode(self) -> str:
        """テーブルヘッダの現在のソート状態から sort_mode を返す。"""
        col = self._table.horizontalHeader().sortIndicatorSection()
        order = self._table.horizontalHeader().sortIndicatorOrder()
        is_desc = (order == Qt.SortOrder.DescendingOrder)
        if col == _COL_PLAYER_PP:
            return "pp_high" if is_desc else "pp_low"
        if col == _COL_RL_AP:
            return "ap_high" if is_desc else "ap_low"
        if col == _COL_PLAYER_ACC:
            return "acc_high" if is_desc else "acc_low"
        if col == _COL_PLAYER_RANK:
            return "rank_high" if is_desc else "rank_low"
        if col == _COL_STARS:
            return "star_desc" if is_desc else "star_asc"
        return "star_asc"

    def _make_export_tag(self) -> str:
        """現在のフィルタ・ソート・検索状態を反映したファイル名タグを返す。

        例: "unplayed_star3-8_pp_high" / "cleared+nf_q_boss_pp_high"
        """
        parts: List[str] = []

        # ステータス
        sts_parts: List[str] = []
        if self._cb_sts_cleared.isChecked():
            sts_parts.append("cleared")
        if self._cb_sts_nf.isChecked():
            sts_parts.append("nf")
        if self._cb_sts_unplayed.isChecked():
            sts_parts.append("unplayed")
        # 全チェックならタグなし（デフォルト）
        if len(sts_parts) < 3:
            parts.append("+".join(sts_parts) if sts_parts else "none")

        # 検索テキスト
        search = self._search_edit.text().strip()
        if search:
            safe = search.replace(" ", "-")[:20]
            parts.append(f"q_{safe}")

        # ★レンジ
        s_min = self._star_min.value()
        s_max = self._star_max.value()
        if s_min > 0.0 or s_max < 20.0:
            parts.append(f"star{s_min:.0f}-{s_max:.0f}")

        # AccSaber / AccSaber RL カテゴリ
        if self._rb_acc.isChecked() or self._rb_acc_rl.isChecked():
            cat_parts: List[str] = []
            if self._cb_cat_true.isChecked():
                cat_parts.append("T")
            if self._cb_cat_standard.isChecked():
                cat_parts.append("S")
            if self._cb_cat_tech.isChecked():
                cat_parts.append("Tc")
            if len(cat_parts) < 3:
                parts.append("+".join(cat_parts) if cat_parts else "nocat")

        # ソート
        sort_mode = self._current_sort_mode()
        _sort_tags = {
            "star_asc": "star_asc", "star_desc": "star_desc",
            "pp_high": "pp_desc", "pp_low": "pp_asc",
            "ap_high": "ap_desc", "ap_low": "ap_asc",
            "acc_high": "acc_desc", "acc_low": "acc_asc",
            "rank_low": "rank_asc", "rank_high": "rank_desc",
        }
        parts.append(_sort_tags.get(sort_mode, sort_mode))

        return "_".join(parts) if parts else "all"

    def _update_sort_label(self) -> None:
        """Export エリアのソート表示ラベルを現在のテーブルソート状態に合わせて更新する。"""
        col = self._table.horizontalHeader().sortIndicatorSection()
        order = self._table.horizontalHeader().sortIndicatorOrder()
        is_desc = (order == Qt.SortOrder.DescendingOrder)
        col_name = _COL_LABELS[col] if 0 <= col < len(_COL_LABELS) else "?"
        arrow = "↓" if is_desc else "↑"
        self._sort_label.setText(f"{col_name} {arrow}")

    def _on_header_clicked(self, _col: int) -> None:
        """ヘッダクリック後にソート表示ラベルを更新する。"""
        self._update_sort_label()

    def _browse_bplist(self) -> None:
        # 前回のエクスポート先または開いたファイルのディレクトリを初期フォルダにする
        current = self._open_edit.text().strip()
        init_dir = str(Path(current).parent) if current and Path(current).exists() else self._export_dir
        path, _ = QFileDialog.getOpenFileName(
            self, "Open bplist file", init_dir,
            "Playlist files (*.bplist *.json);;BeatSaber Playlist (*.bplist);;JSON (*.json);;All files (*)"
        )
        if path:
            self._open_edit.setText(path)
            self._save_export_dir(str(Path(path).parent))
            # ファイル名の先頭でサービスを自動選択
            stem = Path(path).stem.lower()
            if stem.startswith("ss"):
                svc = "scoresaber"
            elif stem.startswith("bl"):
                svc = "beatleader"
            elif stem.startswith("rl") or stem.startswith("accsaber_reloaded"):
                svc = "accsaber_rl"
            elif stem.startswith("accsaber") or stem.startswith("as_"):
                svc = "accsaber"
            else:
                svc = None
            if svc is not None:
                idx = self._svc_combo.findData(svc)
                if idx >= 0:
                    self._svc_combo.setCurrentIndex(idx)

    # ──────────────────────────────────────────────────────────────────────────
    # データ読み込み
    # ──────────────────────────────────────────────────────────────────────────

    def _load_data(self) -> None:
        """選択されたソースに応じてマップデータを読み込む。"""
        self._btn_load.setEnabled(False)
        self._all_entries = []
        self._filtered = []
        self._table.setRowCount(0)

        steam_id = self._steam_id

        if self._rb_ss.isChecked():
            self._setWindowTitle_source(SOURCE_SS)
            try:
                self._all_entries = load_ss_maps(steam_id)
            except Exception as exc:
                QMessageBox.critical(self, "Load Error", str(exc))
                self._btn_load.setEnabled(True)
                return
            self._btn_load.setEnabled(True)
            if not self._all_entries:
                self._count_label.setText("0 maps (no cache)")
                return
            self._apply_filter()

        elif self._rb_bl.isChecked():
            self._setWindowTitle_source(SOURCE_BL)
            try:
                self._all_entries = load_bl_maps(steam_id)
            except Exception as exc:
                QMessageBox.critical(self, "Load Error", str(exc))
                self._btn_load.setEnabled(True)
                return
            self._btn_load.setEnabled(True)
            if not self._all_entries:
                self._count_label.setText("0 maps (no cache)")
                return
            self._apply_filter()

        elif self._rb_acc.isChecked():
            self._setWindowTitle_source(SOURCE_ACC)
            self._start_async_load(lambda sig: self._run_load_acc(sig, steam_id, "all"))

        elif self._rb_acc_rl.isChecked():
            self._setWindowTitle_source(SOURCE_ACC_RL)
            self._start_async_load(lambda sig: self._run_load_acc_rl(sig, steam_id, "all"))

        elif self._rb_open.isChecked():
            file_path_str = self._open_edit.text().strip()
            if not file_path_str:
                QMessageBox.warning(self, "Open File", "Please specify a .bplist or .json file.")
                self._btn_load.setEnabled(True)
                return
            p = Path(file_path_str)
            if not p.exists():
                QMessageBox.warning(self, "Open File", f"File not found:\n{p}")
                self._btn_load.setEnabled(True)
                return
            if p.suffix.lower() not in (".bplist", ".json"):
                QMessageBox.warning(self, "Open File", "Unsupported file type. Please open a .bplist or .json file.")
                self._btn_load.setEnabled(True)
                return
            svc = self._svc_combo.currentData() or "none"
            self._setWindowTitle_source(f"Open: {p.name}")
            if svc == "accsaber_rl":
                # RL は非同期ロード（プログレスバー付き）
                self._open_bplist_path = p
                self._start_async_load(
                    lambda sig: self._run_load_open_rl(sig, p, steam_id)
                )
                return
            try:
                self._all_entries = load_bplist_maps(p, svc, steam_id)
            except Exception as exc:
                QMessageBox.critical(self, "Load Error", str(exc))
                self._btn_load.setEnabled(True)
                return
            self._btn_load.setEnabled(True)
            if not self._all_entries:
                self._count_label.setText("0 maps")
                return
            # open モード時は Acc Category 列を Service 列として使用
            hdr_item = self._table.horizontalHeaderItem(_COL_ACC_CAT)
            if hdr_item is not None:
                hdr_item.setText("Service")
            # プレイリストの曲順で表示するためソートをリセット
            self._table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
            self._apply_filter()

    def _start_async_load(self, worker_fn) -> None:
        """API 取得をスレッドで実行してプログレスダイアログを表示する。"""
        dlg = QProgressDialog("Loading...", "Cancel", 0, 0, self)
        dlg.setWindowTitle("Loading")
        dlg.setMinimumWidth(340)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.show()
        self._progress_dlg = dlg

        sigs = self._load_signals

        def _task() -> None:
            try:
                worker_fn(sigs)
            except Exception as exc:  # noqa: BLE001
                sigs.error.emit(str(exc))

        t = threading.Thread(target=_task, daemon=True)

        def _on_cancel() -> None:
            # キャンセルボタンは UI を閉じるだけ（スレッドは自然終了を待つ）
            dlg.close()
            self._btn_load.setEnabled(True)

        dlg.canceled.connect(_on_cancel)
        t.start()

    def _run_load_open_rl(self, sigs: _LoadSignals, bplist_path: Path, steam_id: Optional[str]) -> None:
        """open + AccSaber RL の非同期ロードタスク。"""
        def _progress(done: int, total: int, label: str) -> None:
            sigs.progress.emit(done, total, label)
        try:
            entries = load_bplist_maps(bplist_path, "accsaber_rl", steam_id, on_progress=_progress)
            sigs.finished.emit(entries)
        except Exception as exc:
            sigs.error.emit(str(exc))

    def _run_load_acc(self, sigs: _LoadSignals, steam_id: Optional[str], category: str) -> None:
        def _progress(done: int, total: int, label: str) -> None:
            sigs.progress.emit(done, total, label)
        try:
            entries = load_accsaber_maps(steam_id, category, on_progress=_progress)
            sigs.finished.emit(entries)
        except Exception as exc:
            sigs.error.emit(str(exc))

    def _run_load_acc_rl(self, sigs: _LoadSignals, steam_id: Optional[str], category: str) -> None:
        def _progress(done: int, total: int, label: str) -> None:
            sigs.progress.emit(done, total, label)
        try:
            entries = load_accsaber_reloaded_maps(steam_id, category, on_progress=_progress)
            sigs.finished.emit(entries)
        except Exception as exc:
            sigs.error.emit(str(exc))

    def _on_load_progress(self, done: int, total: int, label: str) -> None:
        if self._progress_dlg and not self._progress_dlg.wasCanceled():
            if total > 0:
                self._progress_dlg.setMaximum(total)
                self._progress_dlg.setValue(done)
            self._progress_dlg.setLabelText(label)

    def _on_load_finished(self, entries: List[MapEntry]) -> None:
        if self._progress_dlg:
            self._progress_dlg.close()
            self._progress_dlg = None
        self._btn_load.setEnabled(True)
        self._all_entries = entries
        if not entries:
            self._count_label.setText("0 maps")
            return
        # AccSaber RL のときは ★ フィルタをリセット（★が意味を持たないため）
        is_rl = self._rb_acc_rl.isChecked() or (
            self._rb_open.isChecked() and self._svc_combo.currentData() == "accsaber_rl"
        )
        if is_rl:
            self._star_min.blockSignals(True)
            self._star_max.blockSignals(True)
            self._star_min.setValue(0.0)
            self._star_max.setValue(20.0)
            self._star_min.blockSignals(False)
            self._star_max.blockSignals(False)
        # open + AccSaber RL の場合はヘッダを Service に変更
        if self._rb_open.isChecked() and self._svc_combo.currentData() == "accsaber_rl":
            hdr_item = self._table.horizontalHeaderItem(_COL_ACC_CAT)
            if hdr_item is not None:
                hdr_item.setText("Service")
        # Open モード: プレイリストの曲順で表示するためソートをリセット
        if self._rb_open.isChecked():
            self._table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        self._apply_filter()

    def _on_load_error(self, msg: str) -> None:
        if self._progress_dlg:
            self._progress_dlg.close()
            self._progress_dlg = None
        self._btn_load.setEnabled(True)
        QMessageBox.critical(self, "Load Error", msg)

    def _setWindowTitle_source(self, src: str) -> None:
        self.setWindowTitle(f"Playlist — {src}")

    def _open_batch_export(self) -> None:
        pass  # 互換性のため残す（右パネルは常時表示）

    def _load_export_dir(self) -> str:
        """前回のエクスポート先フォルダを読み込む。"""
        try:
            if _EXPORT_DIR_PATH.exists():
                d = json.loads(_EXPORT_DIR_PATH.read_text(encoding="utf-8"))
                path = d.get("export_dir", "")
                if path and Path(path).is_dir():
                    return path
        except Exception:
            pass
        return ""

    def _save_export_dir(self, folder: str) -> None:
        """エクスポート先フォルダを保存する。"""
        self._export_dir = folder
        try:
            _EXPORT_DIR_PATH.write_text(
                json.dumps({"export_dir": folder}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _batch_load_configs(self) -> "List[_BatchConfig]":
        """保存済みバッチ設定を読み込む。"""
        try:
            if _BATCH_CONFIG_PATH.exists():
                data = json.loads(_BATCH_CONFIG_PATH.read_text(encoding="utf-8"))
                return [_BatchConfig.from_dict(d) for d in data]
        except Exception:
            pass
        return []

    def _batch_save_configs(self) -> None:
        """バッチ設定をファイルに保存する。"""
        try:
            _BATCH_CONFIG_PATH.write_text(
                json.dumps([c.to_dict() for c in self._batch_configs], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _batch_refresh_queue(self) -> None:
        """バッチキューの表示を更新する。"""
        self._batch_queue_list.clear()
        for cfg in self._batch_configs:
            self._batch_queue_list.addItem(cfg.display_text())
        n = len(self._batch_configs)
        self._batch_count_label.setText(f"{n} item{'s' if n != 1 else ''}")

    def _batch_remove_selected(self) -> None:
        rows = sorted(
            {self._batch_queue_list.row(s) for s in self._batch_queue_list.selectedItems()},
            reverse=True,
        )
        for r in rows:
            del self._batch_configs[r]
        self._batch_save_configs()
        self._batch_refresh_queue()

    def _batch_clear(self) -> None:
        if not self._batch_configs:
            return
        ans = QMessageBox.question(
            self, "Clear Queue",
            f"Clear all {len(self._batch_configs)} items from the queue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        self._batch_configs.clear()
        self._batch_save_configs()
        self._batch_refresh_queue()

    def _add_to_batch(self) -> None:
        """現在のフィルタ条件をバッチキューに追加する。"""
        if not self._all_entries:
            QMessageBox.information(self, "Add to Batch", "No data loaded.")
            return

        search_text = self._search_edit.text().strip()
        if search_text:
            return

        split = self._rb_exp_split.isChecked()
        is_acc_any = self._rb_acc.isChecked() or self._rb_acc_rl.isChecked()

        if self._rb_ss.isChecked():
            src_tag = "ss"
        elif self._rb_bl.isChecked():
            src_tag = "bl"
        elif self._rb_acc.isChecked():
            src_tag = "acc"
        elif self._rb_open.isChecked():
            svc = self._svc_combo.currentData()
            src_tag = {"scoresaber": "ss", "beatleader": "bl", "accsaber_rl": "rl"}.get(svc, "pl")
        else:
            src_tag = "rl"
        src_label = _BATCH_SRC_PREFIX.get(src_tag, src_tag.upper())
        display_style = ("cat" if is_acc_any else "split") if split else ""
        filename_base = ""
        name = "_".join(p for p in [src_label, display_style] if p)

        split_mode = ("category" if is_acc_any else "star") if split else "single"
        sort_mode = self._current_sort_mode()

        cfg = _BatchConfig(
            label=name,
            filename_base=filename_base,
            source=src_tag,
            show_cleared=self._cb_sts_cleared.isChecked(),
            show_nf=self._cb_sts_nf.isChecked(),
            show_unplayed=self._cb_sts_unplayed.isChecked(),
            cat_true=self._cb_cat_true.isChecked() if is_acc_any else True,
            cat_standard=self._cb_cat_standard.isChecked() if is_acc_any else True,
            cat_tech=self._cb_cat_tech.isChecked() if is_acc_any else True,
            star_min=self._star_min.value(),
            star_max=self._star_max.value(),
            split_mode=split_mode,
            sort_mode=sort_mode,
        )
        self._batch_configs.append(cfg)
        self._batch_save_configs()
        self._batch_refresh_queue()

    def _batch_add_presets(self) -> None:
        """チェックされたプリセットをバッチキューに追加する（即時・データロード不要）。"""
        checked: List[_BatchPreset] = []
        for i in range(self._preset_list_w.count()):
            it = self._preset_list_w.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                checked.append(it.data(Qt.ItemDataRole.UserRole))
        if not checked:
            QMessageBox.information(self, "Add Presets", "No presets checked.")
            return

        for p in checked:
            src_pfx = _BATCH_SRC_PREFIX.get(p.source, p.source.upper())
            if p.source in ("rl", "acc"):
                if p.split_by_star and not p.rl_cat:
                    split_mode = "category"
                    cat_true = cat_standard = cat_tech = True
                else:
                    split_mode = "single"
                    cat_true = p.rl_cat == "true"
                    cat_standard = p.rl_cat == "standard"
                    cat_tech = p.rl_cat == "tech"
            else:
                cat_true = cat_standard = cat_tech = True
                split_mode = "star" if p.split_by_star else "single"
            split_code = {"star": "split", "category": "cat", "single": "single"}.get(split_mode, split_mode)
            batch_label = f"{src_pfx}_{split_code}"

            cfg = _BatchConfig(
                label=batch_label,
                filename_base=p.filename_base,
                source=p.source,
                show_cleared=not p.uncleared,
                show_nf=True,
                show_unplayed=True,
                cat_true=cat_true,
                cat_standard=cat_standard,
                cat_tech=cat_tech,
                split_mode=split_mode,
                sort_mode=p.sort_mode,
            )
            self._batch_configs.append(cfg)

        self._batch_save_configs()
        self._batch_refresh_queue()

    def _on_export_progress(self, done: int, total: int, label: str) -> None:
        if self._batch_progress_dlg and not self._batch_progress_dlg.wasCanceled():
            if total > 0:
                self._batch_progress_dlg.setMaximum(total)
                self._batch_progress_dlg.setValue(done)
            self._batch_progress_dlg.setLabelText(label)

    def _on_export_finished(self, result: list) -> None:
        if self._batch_progress_dlg:
            self._batch_progress_dlg.close()
            self._batch_progress_dlg = None
        self._btn_batch_export_all.setEnabled(True)
        self._btn_quick_export.setEnabled(True)
        saved: List[str] = result[0]
        errors: List[str] = result[1]
        self._show_bplist_covers_dialog(
            f"Export Complete — {len(saved)} file(s)",
            self._export_dir,
            saved,
            errors,
        )

    def _on_export_error(self, msg: str) -> None:
        if self._batch_progress_dlg:
            self._batch_progress_dlg.close()
            self._batch_progress_dlg = None
        self._btn_batch_export_all.setEnabled(True)
        self._btn_quick_export.setEnabled(True)
        QMessageBox.critical(self, "Export Error", msg)

    def _quick_export_presets(self) -> None:
        """チェックされたプリセットをキューに追加せず直接エクスポートする。"""
        checked: List[_BatchPreset] = []
        for i in range(self._preset_list_w.count()):
            it = self._preset_list_w.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                checked.append(it.data(Qt.ItemDataRole.UserRole))
        if not checked:
            QMessageBox.information(self, "Quick Export", "No presets checked.")
            return

        folder = QFileDialog.getExistingDirectory(self, "Select output folder", self._export_dir)
        if not folder:
            return
        self._save_export_dir(folder)

        configs: List[_BatchConfig] = []
        for p in checked:
            src_pfx = _BATCH_SRC_PREFIX.get(p.source, p.source.upper())
            if p.source in ("rl", "acc"):
                if p.split_by_star and not p.rl_cat:
                    split_mode = "category"
                    cat_true = cat_standard = cat_tech = True
                else:
                    split_mode = "single"
                    cat_true = p.rl_cat == "true"
                    cat_standard = p.rl_cat == "standard"
                    cat_tech = p.rl_cat == "tech"
            else:
                cat_true = cat_standard = cat_tech = True
                split_mode = "star" if p.split_by_star else "single"
            split_code = {"star": "split", "category": "cat", "single": "single"}.get(split_mode, split_mode)
            batch_label = f"{src_pfx}_{split_code}"
            configs.append(_BatchConfig(
                label=batch_label,
                filename_base=p.filename_base,
                source=p.source,
                show_cleared=not p.uncleared,
                show_nf=True,
                show_unplayed=True,
                cat_true=cat_true,
                cat_standard=cat_standard,
                cat_tech=cat_tech,
                split_mode=split_mode,
                sort_mode=p.sort_mode,
            ))

        folder_path = Path(folder)
        steam_id = self._steam_id
        sigs = self._export_sigs

        dlg = QProgressDialog("Starting...", "Cancel", 0, len(configs), self)
        dlg.setWindowTitle("Quick Export")
        dlg.setMinimumWidth(420)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.show()
        self._batch_progress_dlg = dlg
        self._btn_quick_export.setEnabled(False)

        def _task() -> None:
            try:
                covers = _pregenerate_covers(configs)
                _run_export_configs(sigs, steam_id, configs, folder_path, covers)
            except Exception as exc:
                sigs.error.emit(str(exc))

        dlg.canceled.connect(lambda: self._btn_quick_export.setEnabled(True))
        threading.Thread(target=_task, daemon=True).start()

    def _batch_export_all(self) -> None:
        """バッチ設定リストの最新データをロードして一括エクスポートする（非同期）。"""
        if not self._batch_configs:
            QMessageBox.information(self, "Export All", "Batch queue is empty.")
            return

        folder = QFileDialog.getExistingDirectory(self, "Select output folder", self._export_dir)
        if not folder:
            return
        self._save_export_dir(folder)

        folder_path = Path(folder)
        configs = list(self._batch_configs)
        steam_id = self._steam_id
        sigs = self._export_sigs

        dlg = QProgressDialog("Starting...", "Cancel", 0, len(configs), self)
        dlg.setWindowTitle("Batch Export")
        dlg.setMinimumWidth(420)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.show()
        self._batch_progress_dlg = dlg
        self._btn_batch_export_all.setEnabled(False)

        def _task() -> None:
            try:
                covers = _pregenerate_covers(configs)
                _run_export_configs(sigs, steam_id, configs, folder_path, covers)
            except Exception as exc:
                sigs.error.emit(str(exc))

        dlg.canceled.connect(lambda: self._btn_batch_export_all.setEnabled(True))
        threading.Thread(target=_task, daemon=True).start()

    # ──────────────────────────────────────────────────────────────────────────
    # カバー画像プレビュー
    # ──────────────────────────────────────────────────────────────────────────

    def _show_bplist_covers_dialog(
        self,
        title: str,
        folder: str,
        filenames: List[str],
        errors: List[str],
    ) -> None:
        """保存済み .bplist ファイルのカバー画像をサムネイルグリッドで表示する。"""
        import base64 as _b64

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(720, 540)

        outer = QVBoxLayout(dlg)
        outer.setSpacing(8)
        outer.setContentsMargins(12, 12, 12, 12)

        summary_lbl = QLabel(f"{len(filenames)} file(s) saved to:\n{folder}")
        summary_lbl.setWordWrap(True)
        outer.addWidget(summary_lbl)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        grid.setSpacing(10)
        grid.setContentsMargins(8, 8, 8, 8)
        scroll.setWidget(grid_widget)

        COLS = 5
        THUMB = 100

        for idx, fname in enumerate(filenames):
            fpath = Path(folder) / fname
            pm = QPixmap()
            try:
                data = json.loads(fpath.read_text(encoding="utf-8"))
                img_data = data.get("image", "")
                if img_data.startswith("data:"):
                    raw = _b64.b64decode(img_data.split(",", 1)[1])
                    pm.loadFromData(raw)
            except Exception:
                pass

            img_lbl = QLabel()
            if not pm.isNull():
                img_lbl.setPixmap(
                    pm.scaled(THUMB, THUMB,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )
            else:
                img_lbl.setText("(no image)")
            img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            img_lbl.setFixedSize(THUMB + 4, THUMB + 4)
            img_lbl.setStyleSheet("border: 1px solid #555; background: #1a1a1a;")

            short = fname if len(fname) <= 22 else fname[:10] + "…" + fname[-10:]
            name_lbl = QLabel(short)
            name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            name_lbl.setWordWrap(True)
            name_lbl.setMaximumWidth(THUMB + 4)
            name_lbl.setToolTip(fname)

            cell = QWidget()
            cell_layout = QVBoxLayout(cell)
            cell_layout.setSpacing(2)
            cell_layout.setContentsMargins(0, 0, 0, 0)
            cell_layout.addWidget(img_lbl)
            cell_layout.addWidget(name_lbl)

            r, c = divmod(idx, COLS)
            grid.addWidget(cell, r, c)

        outer.addWidget(scroll, 1)

        if errors:
            err_lbl = QLabel("Errors:\n" + "\n".join(errors[:10]))
            err_lbl.setStyleSheet("color: #ff6666;")
            err_lbl.setWordWrap(True)
            outer.addWidget(err_lbl)

        btn_ok = QPushButton("OK")
        btn_ok.setDefault(True)
        btn_ok.clicked.connect(dlg.accept)
        outer.addWidget(btn_ok, 0, Qt.AlignmentFlag.AlignRight)

        dlg.exec()

    def _show_cover_preview(self) -> None:
        """出力フォルダを選択して .bplist ファイルのカバー画像を一覧表示する。"""
        folder = QFileDialog.getExistingDirectory(
            self, "Select export folder to preview", self._export_dir
        )
        if not folder:
            return
        bplist_files = sorted(f.name for f in Path(folder).glob("*.bplist"))
        if not bplist_files:
            QMessageBox.information(self, "Preview", "No .bplist files found in the selected folder.")
            return
        self._show_bplist_covers_dialog(f"Cover Preview — {Path(folder).name}", folder, bplist_files, [])

    # ──────────────────────────────────────────────────────────────────────────
    # フィルタ
    # ──────────────────────────────────────────────────────────────────────────

    def _apply_filter(self) -> None:
        """フィルタ条件に従ってテーブルを更新する。"""
        text = self._search_edit.text().strip().lower()
        star_min = self._star_min.value()
        star_max = self._star_max.value()
        show_cleared = self._cb_sts_cleared.isChecked()
        show_nf = self._cb_sts_nf.isChecked()
        show_unplayed = self._cb_sts_unplayed.isChecked()
        rl_mode = self._rb_acc.isChecked() or self._rb_acc_rl.isChecked()
        cat_filter: Optional[set] = None
        if rl_mode:
            allowed: set = set()
            if self._cb_cat_true.isChecked():
                allowed.add("true")
            if self._cb_cat_standard.isChecked():
                allowed.add("standard")
            if self._cb_cat_tech.isChecked():
                allowed.add("tech")
            cat_filter = allowed

        result: List[MapEntry] = []
        for e in self._all_entries:
            # 星フィルタ
            if e.stars < star_min or e.stars > star_max:
                continue
            # テキストフィルタ
            if text and not any(
                text in f.lower()
                for f in [e.song_name, e.song_author, e.mapper]
            ):
                continue
            # ステータスフィルタ
            if e.cleared and not show_cleared:
                continue
            if e.nf_clear and not show_nf:
                continue
            if not e.played and not show_unplayed:
                continue
            # カテゴリフィルタ (AccSaber RL)
            if cat_filter is not None and e.acc_category not in cat_filter:
                continue
            result.append(e)

        self._filtered = result
        self._count_label.setText(f"{len(result):,} maps")
        self._refresh_table(result)

    # ──────────────────────────────────────────────────────────────────────────
    # テーブル更新
    # ──────────────────────────────────────────────────────────────────────────

    def _refresh_table(self, entries: List[MapEntry]) -> None:
        table = self._table
        table.setSortingEnabled(False)
        table.setRowCount(0)
        table.setRowCount(len(entries))

        _cleared_bg = QColor(0x26, 0x49, 0x30, 180) if is_dark() else QColor(0xC8, 0xE6, 0xC9)
        _nf_bg = QColor(0x5C, 0x4A, 0x1A, 180) if is_dark() else QColor(0xFF, 0xF3, 0xCD)
        _unplayed_bg = QColor(0x4A, 0x2A, 0x2A, 180) if is_dark() else QColor(0xFF, 0xCC, 0xCC)
        _is_acc_mode = self._rb_acc.isChecked() or self._rb_acc_rl.isChecked() or (
            self._rb_open.isChecked() and self._svc_combo.currentData() == "accsaber_rl"
        )

        for row, e in enumerate(entries):
            # ステータス
            status_item = QTableWidgetItem(e.status_str)
            status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if e.cleared:
                status_item.setBackground(_cleared_bg)
            elif e.nf_clear:
                status_item.setBackground(_nf_bg)
            else:
                status_item.setBackground(_unplayed_bg)
            table.setItem(row, _COL_STATUS, status_item)

            # 曲名
            table.setItem(row, _COL_SONG, QTableWidgetItem(e.song_name))
            # 難易度
            table.setItem(row, _COL_DIFF, _diff_item(e.difficulty))
            # モード
            table.setItem(row, _COL_MODE, _mode_item(e.mode))
            # ★ / Complexity
            if _is_acc_mode:
                star_item = _NumItem(f"{e.acc_complexity:.1f}" if e.acc_complexity > 0 else "-", e.acc_complexity)
                star_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, _COL_STARS, star_item)
            else:
                star_item = _NumItem(f"{e.stars:.2f}", e.stars)
                star_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row, _COL_STARS, star_item)
            # Player PP
            pp_item = _NumItem(
                f"{e.player_pp:.1f}" if e.player_pp > 0 else "-",
                e.player_pp,
            )
            pp_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, _COL_PLAYER_PP, pp_item)
            # RL AP
            ap_item = _NumItem(
                f"{e.acc_rl_ap:.2f}" if e.acc_rl_ap > 0 else "-",
                e.acc_rl_ap,
            )
            ap_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, _COL_RL_AP, ap_item)
            # Acc
            acc_item = _NumItem(
                f"{e.player_acc:.2f}%" if e.player_acc > 0 else "-",
                e.player_acc,
            )
            acc_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, _COL_PLAYER_ACC, acc_item)
            # Rank
            rank_item = _NumItem(
                str(e.player_rank) if e.player_rank > 0 else "-",
                e.player_rank if e.player_rank > 0 else 999_999_999,
            )
            rank_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(row, _COL_PLAYER_RANK, rank_item)
            # 作曲者
            table.setItem(row, _COL_AUTHOR, QTableWidgetItem(e.song_author))
            # マッパー
            table.setItem(row, _COL_MAPPER, QTableWidgetItem(e.mapper))
            # Mod (SC, NF, etc.)
            mod_item = QTableWidgetItem(e.player_mods)
            mod_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            table.setItem(row, _COL_MOD, mod_item)
            # Acc区分 / Service (openモード時はサービスマッチ表示)
            if e.source in ("scoresaber", "beatleader", "accsaber_reloaded"):
                svc_label = {"scoresaber": "SS", "beatleader": "BL", "accsaber_reloaded": "RL"}.get(e.source, e.source)
                if e.source == "accsaber_reloaded" and e.acc_category:
                    _cat_disp = {"true": "True", "standard": "Standard", "tech": "Tech"}.get(e.acc_category, e.acc_category)
                    svc_label = f"RL/{_cat_disp}"
            elif e.source == "open":
                svc_label = "-"
            else:
                svc_label = ""
            cat_text = svc_label if self._rb_open.isChecked() else e.acc_category
            table.setItem(row, _COL_ACC_CAT, QTableWidgetItem(cat_text))

        table.setSortingEnabled(True)

    # ──────────────────────────────────────────────────────────────────────────
    # 一括出力
    # ──────────────────────────────────────────────────────────────────────────

    def _on_export(self) -> None:
        """Style ラジオに応じて出力メソッドを呼ぶ。"""
        split = self._rb_exp_split.isChecked()
        acc_source = self._rb_acc_rl.isChecked()
        tag = self._make_export_tag()

        # Open モード時は元ファイル名をベースにする
        if self._rb_open.isChecked():
            src_path = self._open_edit.text().strip()
            stem = Path(src_path).stem if src_path else "export"
            tag = f"{stem}_{tag}"

        if split and acc_source:
            self._export_by_category(list(self._filtered), tag)
        elif split:
            self._export_per_star_all(tag)
        else:
            self._export_all_by_pp(tag)

    def _export_all_by_pp(self, tag: str = "all") -> None:
        """全マップを ★ → Player PP 降順で 1 つの bplist に出力する。"""
        target = list(self._filtered)
        sorted_entries = sorted(
            target,
            key=lambda e: (math.floor(e.stars), -e.player_pp, e.song_name),
        )

        if not sorted_entries:
            QMessageBox.information(self, "Export", "No maps found.")
            return

        title = f"Maps ({tag})"
        _sort_dir = _sort_dir_from_mode(self._current_sort_mode())
        src = "ss" if self._rb_ss.isChecked() else "bl" if self._rb_bl.isChecked() else "rl"
        image = _make_playlist_cover("default", "", _sort_dir, src)
        saved = _save_bplist(self, title, sorted_entries, self._export_dir, image)
        if saved:
            folder = str(Path(saved).parent)
            self._save_export_dir(folder)
            self._show_bplist_covers_dialog(
                "Export Complete", folder, [Path(saved).name], []
            )

    # ── ★別分割出力 共通ヘルパー ─────────────────────────────────────────

    def _group_by_star(self, entries: List[MapEntry]) -> Dict[int, List[MapEntry]]:
        """MapEntry のリストを ★ の整数値でグループ化する。"""
        groups: Dict[int, List[MapEntry]] = {}
        for e in entries:
            star_int = max(1, math.floor(e.stars)) if e.stars > 0 else 0
            groups.setdefault(star_int, []).append(e)
        return groups

    def _export_per_star(
        self,
        entries: List[MapEntry],
        filename_suffix: str,
        title_template: str,
    ) -> None:
        """★別に分割して bplist ファイルをフォルダに一括保存する。

        filename_suffix: ファイル名の末尾タグ
        title_template: {star} を含むプレイリストタイトルテンプレート
        """
        if not entries:
            QMessageBox.information(self, "Export", "No maps found.")
            return

        folder = QFileDialog.getExistingDirectory(self, "Select output folder", self._export_dir)
        if not folder:
            return
        self._save_export_dir(folder)

        groups = self._group_by_star(entries)
        saved_fnames: List[str] = []
        errors: List[str] = []
        _sort_dir = _sort_dir_from_mode(self._current_sort_mode())

        for star_int in sorted(groups.keys()):
            group_entries = sorted(
                groups[star_int],
                key=lambda e: (e.stars, -e.player_pp, e.song_name),
            )
            title = title_template.format(star=star_int)
            filename = f"{star_int:02d}star_{filename_suffix}.bplist"
            out_path = Path(folder) / filename
            image = _make_playlist_cover("star", str(star_int), _sort_dir)
            bplist = _make_bplist(title, group_entries, image)
            try:
                out_path.write_text(
                    json.dumps(bplist, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                saved_fnames.append(filename)
            except Exception as exc:
                errors.append(f"★{star_int}: {exc}")

        self._show_bplist_covers_dialog(
            f"Export Complete — {len(saved_fnames)} file(s)",
            folder,
            saved_fnames,
            errors,
        )

    def _export_per_star_all(self, tag: str = "all") -> None:
        """全マップを ★ ごとに別ファイル (PP 降順) で出力する。"""
        self._export_per_star(
            list(self._filtered),
            filename_suffix=tag,
            title_template="{star}★ " + tag,
        )

    def _export_by_category(self, entries: List[MapEntry], tag: str = "all") -> None:
        """acc_category ごとに別ファイルで出力する (AccSaber / AccSaber Reloaded 用)。"""
        if not entries:
            QMessageBox.information(self, "Export", "No maps found.")
            return

        folder = QFileDialog.getExistingDirectory(self, "Select output folder", self._export_dir)
        if not folder:
            return
        self._save_export_dir(folder)

        folder_path = Path(folder)
        groups: Dict[str, List[MapEntry]] = {}
        for e in entries:
            cat = e.acc_category or "unknown"
            groups.setdefault(cat, []).append(e)

        saved_fnames: List[str] = []
        errors: List[str] = []
        _sort_dir = _sort_dir_from_mode(self._current_sort_mode())

        for cat in sorted(groups.keys()):
            try:
                cat_entries = sorted(groups[cat], key=lambda e: (e.stars, e.song_name))
                fname = folder_path / f"{cat}_{tag}.bplist"
                image = _make_playlist_cover(cat, "", _sort_dir)
                bplist = _make_bplist(f"{cat.capitalize()} ({tag})", cat_entries, image)
                fname.write_text(
                    json.dumps(bplist, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                saved_fnames.append(fname.name)
            except Exception as exc:
                errors.append(f"{cat}: {exc}")

        self._show_bplist_covers_dialog(
            f"Export Complete — {len(saved_fnames)} file(s)",
            folder,
            saved_fnames,
            errors,
        )
