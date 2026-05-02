"""PlaylistWindow の永続 state 読み書きを扱う。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from ..snapshot import BASE_DIR


PLAYLIST_WINDOW_PATH = BASE_DIR / "cache" / "playlist_window.json"


def has_saved_playlist_window_state(path: Path = PLAYLIST_WINDOW_PATH) -> bool:
    """保存済み state ファイルの存在だけを返す。"""
    return path.exists()


def load_playlist_window_payload(path: Path = PLAYLIST_WINDOW_PATH) -> Dict[str, Any]:
    """PlaylistWindow の state JSON を安全に読み込む。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_playlist_window_payload(payload: Dict[str, Any], path: Path = PLAYLIST_WINDOW_PATH) -> None:
    """PlaylistWindow の state JSON を UTF-8 で保存する。"""
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")