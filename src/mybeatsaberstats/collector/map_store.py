from __future__ import annotations
import json

from pathlib import Path
from typing import Optional


class MapStore:
    """
    マップやスコアの情報を保持するシングルトンクラス
    """
    _instance = None

    snapshots: dict = {}  # スナップショットデータキャッシュ
    acc_players: dict = {}  # AccSaber プレイヤーデータキャッシュ
    ss_basic_info: dict = {}  # ScoreSaber 基本情報キャッシュ ScoreSaberPlayer
    bl_basic_info: dict = {}  # BeatLeader 基本情報キャッシュ BeatLeaderPlayer
    
    player_index: dict = {}  # プレイヤーインデックスキャッシュ
    ss_players: dict = {}  # ScoreSaber プレイヤーデータキャッシュ
    bl_players: dict = {}  # BeatLeader プレイヤーデータキャッシュ

    ss_ranked_maps: dict = {}  # ScoreSaber Ranked Maps キャッシュ
    bl_ranked_maps: dict = {}  # BeatLeader Ranked Maps キャッシュ

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            # まだインスタンスがなければ作成
            cls._instance = super().__new__(cls)
        return cls._instance
