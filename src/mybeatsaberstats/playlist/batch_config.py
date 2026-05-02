"""Batch Export 設定の定義と永続化を扱う。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List

from ..snapshot import BASE_DIR


BATCH_CONFIG_PATH = BASE_DIR / "cache" / "batch_configs.json"


@dataclass
class BatchConfig:
    """バッチエクスポート1件分の設定を保持する。"""

    label: str
    filename_base: str
    source: str
    show_cleared: bool = True
    show_nf: bool = True
    show_unplayed: bool = True
    show_queued: bool = False
    cat_true: bool = True
    cat_standard: bool = True
    cat_tech: bool = True
    star_min: float = 0.0
    star_max: float = 20.0
    highest_diff_only: bool = False
    split_mode: str = "star"
    sort_mode: str = "star_asc"
    song_filter: str = ""
    bs_query: str = ""
    bs_date_mode: str = "days"
    bs_from_date: str = ""
    bs_to_date: str = ""
    bs_days: int = 7
    bs_max_maps: int = 1000
    bs_min_rating: int = 50
    bs_min_votes: int = 0
    mapper_played_min: int = 0
    bs_unranked_only: bool = True
    bs_exclude_ai: bool = True
    enabled: bool = True

    def to_dict(self) -> dict:
        """JSON 保存用の辞書へ変換する。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BatchConfig":
        """未知キーを無視して安全に復元する。"""
        known = {field.name for field in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in data.items() if key in known})

    def display_text(self) -> str:
        """Batch Queue に表示する短い説明文字列を返す。"""
        src = self.source.upper()
        if self.source == "rl":
            cats = [name for flag, name in [(self.cat_true, "Tr"), (self.cat_standard, "Std"), (self.cat_tech, "Tch")] if flag]
            src = f"RL[{'+'.join(cats) or 'none'}]"
        elif self.source == "acc":
            cats = [name for flag, name in [(self.cat_true, "Tr"), (self.cat_standard, "Std"), (self.cat_tech, "Tch")] if flag]
            src = f"Acc[{'+'.join(cats) or 'none'}]"
        elif self.source == "bs":
            parts = [f"R{self.bs_min_rating}", f"V{self.bs_min_votes}", f"Max{self.bs_max_maps}"]
            if self.mapper_played_min > 0:
                parts.append(f"MP{self.mapper_played_min}")
            src = f"BS[{','.join(parts)}]"
        status = "".join(
            text
            for flag, text in [
                (self.show_cleared, "✓"),
                (self.show_nf, "⚠"),
                (self.show_unplayed, "✗"),
                (self.show_queued, "Q"),
            ]
            if flag
        )
        filter_parts = [status] if status else []
        if self.highest_diff_only:
            filter_parts.append("TopDiff")
        filter_label = "+".join(filter_parts) if filter_parts else "none"
        sort_label = _display_sort_label(self.sort_mode)
        query_tag = f" 🔍\"{self.song_filter}\"" if self.song_filter else ""
        if self.source in ("rl", "acc"):
            return f"{self.label}  [{src} / {filter_label} / {self.split_mode} / {sort_label}]{query_tag}"
        if self.source == "bs":
            if self.bs_date_mode == "none":
                date_tag = "All dates"
            elif self.bs_date_mode == "dates" and self.bs_from_date and self.bs_to_date:
                date_tag = f"{self.bs_from_date}..{self.bs_to_date}"
            else:
                date_tag = f"Last {self.bs_days}d"
            return f"{self.label}  [{src} / {date_tag} / {filter_label} / {self.split_mode} / {sort_label}]{query_tag}"
        if self.split_mode in ("week", "month"):
            return f"{self.label}  [{src} / {filter_label} / {self.split_mode} / {sort_label}]{query_tag}"
        star = f"★{self.star_min:g}-{self.star_max:g}"
        return f"{self.label}  [{src} / {filter_label} / {star} / {self.split_mode} / {sort_label}]{query_tag}"


def load_playlist_batch_configs(path: Path = BATCH_CONFIG_PATH) -> List[BatchConfig]:
    """保存済みの Batch Export 設定を読み込む。"""
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return [BatchConfig.from_dict(item) for item in data]
    except Exception:
        pass
    return []


def load_enabled_playlist_batch_configs(path: Path = BATCH_CONFIG_PATH) -> List[BatchConfig]:
    """有効化されている設定だけを返す。"""
    return [config for config in load_playlist_batch_configs(path) if config.enabled]


def save_playlist_batch_configs(configs: List[BatchConfig], path: Path = BATCH_CONFIG_PATH) -> None:
    """Batch Export 設定一覧を JSON として保存する。"""
    path.write_text(json.dumps([config.to_dict() for config in configs], ensure_ascii=False, indent=2), encoding="utf-8")


def _display_sort_label(sort_mode: str) -> str:
    """Queue 表示用のソート名を短く整形する。"""
    labels: Dict[str, str] = {
        "star_asc": "Star↑",
        "star_desc": "Star↓",
        "pp_high": "PP↓",
        "pp_low": "PP↑",
        "ap_high": "AP↓",
        "ap_low": "AP↑",
        "acc_high": "Acc↓",
        "acc_low": "Acc↑",
        "rank_low": "Rank↑",
        "rank_high": "Rank↓",
        "bs_rate_high": "Rate↓",
        "bs_rate_low": "Rate↑",
        "bs_upvotes_high": "⇧Votes↓",
        "bs_upvotes_low": "⇧Votes↑",
        "bs_downvotes_high": "⇩Votes↓",
        "bs_downvotes_low": "⇩Votes↑",
        "fc_desc": "FC↓",
        "fc_asc": "FC↑",
        "status_desc": "Sts↓",
        "status_asc": "Sts↑",
        "song_desc": "Song↓",
        "song_asc": "Song↑",
        "date_desc": "Date↓",
        "date_asc": "Date↑",
        "duration_desc": "Len↓",
        "duration_asc": "Len↑",
        "bl_watched_desc": "BLWatched↓",
        "bl_watched_asc": "BLWatched↑",
        "bl_mapper_played_desc": "MapperPlayed↓",
        "bl_mapper_played_asc": "MapperPlayed↑",
        "bl_maps_played_desc": "BLPlayed↓",
        "bl_maps_played_asc": "BLPlayed↑",
        "bl_maps_watched_desc": "BLWatched↓",
        "bl_maps_watched_asc": "BLWatched↑",
        "diff_desc": "Diff↓",
        "diff_asc": "Diff↑",
        "mode_desc": "Mode↓",
        "mode_asc": "Mode↑",
        "cat_desc": "Cat↓",
        "cat_asc": "Cat↑",
        "mapper_desc": "Mapper↓",
        "mapper_asc": "Mapper↑",
        "author_desc": "Author↓",
        "author_asc": "Author↑",
        "playtime_desc": "Played↓",
        "playtime_asc": "Played↑",
    }
    return labels.get(sort_mode, sort_mode)