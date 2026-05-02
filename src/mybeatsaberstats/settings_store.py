from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .snapshot import BASE_DIR


_CACHE_DIR = BASE_DIR / "cache"
_SETTINGS_PATH = _CACHE_DIR / "settings.json"
_LEGACY_EXPORT_DIR_PATH = _CACHE_DIR / "export_dir.json"
_LEGACY_BEATSABER_DIR_PATH = _CACHE_DIR / "beatsaber_dir.json"
_LEGACY_TAKE_SNAPSHOT_DIALOG_PATH = _CACHE_DIR / "take_snapshot_dialog.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def load_settings() -> dict[str, Any]:
    return _read_json(_SETTINGS_PATH)


def save_settings(updates: dict[str, Any]) -> None:
    try:
        payload = load_settings()
        payload.update(updates)
        _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _load_valid_dir_setting(key: str, legacy_path: Path, legacy_key: str) -> str:
    settings = load_settings()
    path = str(settings.get(key, "") or "").strip()
    if path and Path(path).is_dir():
        return path

    legacy = _read_json(legacy_path)
    path = str(legacy.get(legacy_key, "") or "").strip()
    if path and Path(path).is_dir():
        save_settings({key: path})
        return path
    return ""


def _default_playlist_export_dir() -> str:
    beatsaber_dir = load_beatsaber_dir()
    if not beatsaber_dir:
        return ""
    return str(Path(beatsaber_dir) / "Playlists" / "MyBeatSaberStats")


def load_playlist_export_dir() -> str:
    settings = load_settings()
    path = str(settings.get("playlist_export_dir", "") or "").strip()
    if path:
        return path

    legacy = _read_json(_LEGACY_EXPORT_DIR_PATH)
    path = str(legacy.get("export_dir", "") or "").strip()
    if path:
        save_settings({"playlist_export_dir": path})
        return path

    return _default_playlist_export_dir()


def save_playlist_export_dir(folder: str) -> None:
    save_settings({"playlist_export_dir": folder.strip()})


def load_beatsaber_dir() -> str:
    return _load_valid_dir_setting(
        "beatsaber_dir",
        _LEGACY_BEATSABER_DIR_PATH,
        "beatsaber_dir",
    )


def save_beatsaber_dir(folder: str) -> None:
    save_settings({"beatsaber_dir": folder.strip()})


def load_export_all_after_snapshot() -> bool:
    settings = load_settings()
    if "export_all_after_snapshot" in settings:
        return bool(settings.get("export_all_after_snapshot", False))

    legacy = _read_json(_LEGACY_TAKE_SNAPSHOT_DIALOG_PATH)
    enabled = bool(legacy.get("export_all_after_snapshot", False))
    if enabled:
        save_settings({"export_all_after_snapshot": enabled})
    return enabled


def save_export_all_after_snapshot(enabled: bool) -> None:
    save_settings({"export_all_after_snapshot": bool(enabled)})


def load_mapper_load_new_after_maps() -> bool:
    settings = load_settings()
    return bool(settings.get("mapper_load_new_after_maps", True))


def save_mapper_load_new_after_maps(enabled: bool) -> None:
    save_settings({"mapper_load_new_after_maps": bool(enabled)})