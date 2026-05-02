from __future__ import annotations

from typing import Any, Optional


class MapStore:
    """
    マップやスコアの情報を保持するシングルトンクラス
    """
    _instance: Optional[MapStore] = None

    snapshots: dict[str, Any] = {}  # スナップショットデータキャッシュ
    acc_players: dict[str, Any] = {}  # AccSaber プレイヤーデータキャッシュ
    ss_basic_info: dict[str, Any] = {}  # ScoreSaber 基本情報キャッシュ ScoreSaberPlayer
    bl_basic_info: dict[str, Any] = {}  # BeatLeader 基本情報キャッシュ BeatLeaderPlayer
    
    player_index: dict[str, Any] = {}  # プレイヤーインデックスキャッシュ
    ss_players: dict[str, Any] = {}  # ScoreSaber プレイヤーデータキャッシュ
    bl_players: dict[str, Any] = {}  # BeatLeader プレイヤーデータキャッシュ

    ss_ranked_maps: dict[str, Any] = {}  # ScoreSaber Ranked Maps キャッシュ
    bl_ranked_maps: dict[str, Any] = {}  # BeatLeader Ranked Maps キャッシュ

    def __new__(cls, *args, **kwargs) -> MapStore:
        if cls._instance is None:
            # まだインスタンスがなければ作成
            cls._instance = super().__new__(cls)
        return cls._instance
