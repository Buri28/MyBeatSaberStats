"""ファイルの書き込み（途中で止まっても壊れないようにする）。

キャッシュ JSON を path.write_text で直接書くと、書き込み中にアプリを閉じたり別スレッドが読んだりしたとき、
途中までのファイルが残る。壊れた JSON は読むと空扱いになり、次回が全件取得になってしまう。
ここでは同じフォルダの一時ファイルに書いてから os.replace で置き換える（置き換えは一瞬で、途中の状態が見えない）。
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path


def recently_marked(path: Path | str, key: str, hours: float) -> bool:
    """path の JSON に key の時刻が記録されていて、hours 以内かどうか。"""
    import json
    from datetime import datetime, timedelta, timezone

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        value = data.get(key) if isinstance(data, dict) else None
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except Exception:  # noqa: BLE001
        return False
    if when is None:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when < timedelta(hours=hours)


def mark_now(path: Path | str, key: str) -> None:
    """path の JSON に key = 今の時刻（UTC）を記録する。"""
    import json
    from datetime import datetime, timezone

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:  # noqa: BLE001
        data = {}
    data[key] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    except Exception:  # noqa: BLE001
        pass


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    """data を path に書く。書き終わるまで元のファイルはそのまま残る（画像など）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    """text を path に書く。書き終わるまで元のファイルはそのまま残る。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # Windows では別の処理が読んでいる最中だと置き換えに失敗するので、少し待って試し直す
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
