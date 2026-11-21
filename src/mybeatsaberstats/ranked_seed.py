"""配布物に同梱したランク譜面一覧（ranked_seed）を、利用者の cache/ に取り込む。

ランク譜面一覧（ScoreSaber 441 ページ・BeatLeader 40 ページ）を全件取得すると約 200 秒かかる。
新しく使い始めた人・アップデートした人（v0.8.6 以前は ranked_full_traversal.json に {service}_complete が無い）が
毎回これを行わないよう、開発者が全件取得を最後まで終えた一覧を _internal/resources/ranked_seed/ に入れて配り、
Snapshot のランク譜面の取得の前にここで取り込む。取り込んだ後は、同梱分の fetched_at からの差分取得になる。

サービス（scoresaber / beatleader）ごとに、次のどれかに当たるときだけ同梱分で置き換える。
- 利用者の cache/ に一覧が無い
- 利用者の ranked_full_traversal.json に {service}_complete が無い
- 利用者の一覧の fetched_at が同梱分より古い
同じ同梱分（fetched_at が同じ）は 2 回取り込まない（{service}_seed_imported に同梱分の fetched_at を残す）。
利用者の {service}_stars_drift は消さない（★ずれの印があれば、取り込んだ後も全件取り直しになる）。
scoresaber_reweights.json は、SS を取り込んだときに利用者のものが無い場合だけコピーする。
失敗しても例外は外に出さずにログを 1 行出し、今までどおり取得を続ける（キャンセルだけは外に出す）。
"""
from __future__ import annotations

import json
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import ranked_sync
from .fileio import atomic_write_text
from .snapshot import BASE_DIR, RESOURCES_DIR

SEED_DIR_NAME = "ranked_seed"
TRAVERSAL_FILE_NAME = "ranked_full_traversal.json"
REWEIGHTS_FILE_NAME = "scoresaber_reweights.json"
#: サービスごとのランク譜面一覧のファイル名。
LIST_FILE_NAMES = {
    "scoresaber": "scoresaber_ranked_maps.json",
    "beatleader": "beatleader_ranked_maps.json",
}
#: 取り込みのログ（frozen 版は print が出ないので、ファイルにも書く）。
LOG_FILE_NAME = "ranked_seed.log"

# 一覧の先頭にある "fetched_at" を、30MB の JSON を全部読まずに拾う（無ければ全部読む）。
_FETCHED_AT_RE = re.compile(rb'"fetched_at"\s*:\s*"([^"]+)"')
_HEAD_BYTES = 4096


#: 判定から書き込みまでを 1 回にまとめる（Snapshot とプレイリスト画面の mapper 更新が同時に取り込まないように）。
_IMPORT_LOCK = threading.Lock()


def default_seed_dir() -> Path:
    """同梱分の場所。frozen 版で exe の隣に resources があると RESOURCES_DIR はそちらを指すので、
    _internal（_MEIPASS）側の resources も探す。"""
    candidates = [RESOURCES_DIR / SEED_DIR_NAME]
    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and meipass:
        candidates.append(Path(meipass) / "resources" / SEED_DIR_NAME)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def _log(message: str, log_dir: Optional[Path]) -> None:
    line = f"Ranked seed: {message}"
    print(line)
    if log_dir is None:
        return
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / LOG_FILE_NAME).open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {line}\n")
    except Exception:  # noqa: BLE001
        pass


def _parse_utc(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _read_fetched_at(path: Path) -> Optional[str]:
    """一覧の fetched_at（文字列）。無い・読めないときは None。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(_HEAD_BYTES)
        match = _FETCHED_AT_RE.search(head)
        if match:
            return match.group(1).decode("utf-8")
        raw = json.loads(path.read_text(encoding="utf-8"))
        value = raw.get("fetched_at") if isinstance(raw, dict) else None
        return value if isinstance(value, str) and value else None
    except Exception:  # noqa: BLE001
        return None


def _read_json_dict(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} が JSON のオブジェクトではありません")
    return data


def _validate_seed_list(service: str, text: str) -> None:
    """同梱分の一覧が、collector が読める形か（壊れていたら例外）。"""
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("fetched_at"), str):
        raise ValueError(f"{service}: 一覧に fetched_at がありません")
    if service == "scoresaber":
        if not isinstance(data.get("leaderboards"), dict) or not data["leaderboards"]:
            raise ValueError("scoresaber: 一覧に leaderboards がありません")
    else:
        if not isinstance(data.get("pages"), list) or not data["pages"]:
            raise ValueError("beatleader: 一覧に pages がありません")


def _import_service(service: str, seed_dir: Path, cache_dir: Path, seed_traversal: dict, log_dir: Optional[Path]) -> str:
    """1 サービス分を取り込む。戻り値は結果（テスト・ログ用）。"""
    file_name = LIST_FILE_NAMES[service]
    seed_list = seed_dir / file_name
    user_list = cache_dir / file_name
    traversal_path = cache_dir / TRAVERSAL_FILE_NAME

    seed_complete = seed_traversal.get(f"{service}_complete")
    if not seed_complete:
        _log(f"{service}: 同梱分に {service}_complete が無いので取り込みません", log_dir)
        return "seed_incomplete"
    if not seed_list.is_file():
        _log(f"{service}: 同梱分の一覧が無いので取り込みません", log_dir)
        return "seed_missing"
    seed_fetched_text = _read_fetched_at(seed_list)
    seed_fetched = _parse_utc(seed_fetched_text)
    if seed_fetched is None:
        _log(f"{service}: 同梱分の一覧の fetched_at を読めないので取り込みません", log_dir)
        return "seed_broken"

    user_traversal = ranked_sync._read(traversal_path)
    user_fetched = _parse_utc(_read_fetched_at(user_list)) if user_list.is_file() else None
    # 同じ同梱分は 2 回取り込まない。ただし一覧が消えた・読めない（fetched_at が無い）ときは、
    # そのままだと全件取得になるので取り込み直す。
    if user_traversal.get(f"{service}_seed_imported") == seed_fetched_text and user_fetched is not None:
        return "already_imported"

    if not user_list.is_file():
        reason = "一覧が無い"
    elif not user_traversal.get(f"{service}_complete"):
        reason = f"{service}_complete が無い"
    elif user_fetched is None or user_fetched < seed_fetched:
        reason = "一覧が同梱分より古い"
    else:
        return "user_newer"

    text = seed_list.read_text(encoding="utf-8")
    _validate_seed_list(service, text)
    atomic_write_text(user_list, text)

    with ranked_sync._LOCK:
        data = ranked_sync._read(traversal_path)
        data[f"{service}_complete"] = seed_complete
        data[service] = seed_traversal.get(service) or seed_complete
        data[f"{service}_last_ok"] = bool(seed_traversal.get(f"{service}_last_ok", True))
        # 総数の食い違いの記録は試行日時（{service}）と組で使うので、同梱分の値にそろえる
        data[f"{service}_unresolved_total"] = seed_traversal.get(f"{service}_unresolved_total")
        data[f"{service}_unresolved_count"] = int(seed_traversal.get(f"{service}_unresolved_count") or 0)
        data[f"{service}_seed_imported"] = seed_fetched_text
        # {service}_stars_drift は消さない（利用者の側で見つけた★のずれは残す）
        atomic_write_text(traversal_path, json.dumps(data, ensure_ascii=False, indent=2))

    if service == "scoresaber":
        seed_reweights = seed_dir / REWEIGHTS_FILE_NAME
        user_reweights = cache_dir / REWEIGHTS_FILE_NAME
        if seed_reweights.is_file() and not user_reweights.exists():
            try:
                reweights_text = seed_reweights.read_text(encoding="utf-8")
                json.loads(reweights_text)
                atomic_write_text(user_reweights, reweights_text)
            except Exception as exc:  # noqa: BLE001
                # 一覧は取り込めているので結果は imported のまま（リウェイトは wiki から読み直すだけ）
                _log(f"scoresaber: {REWEIGHTS_FILE_NAME} をコピーできませんでした（{exc}）", log_dir)

    _log(f"{service}: 同梱の一覧（fetched_at {seed_fetched_text}）を取り込みました（理由：{reason}）", log_dir)
    return "imported"


def import_ranked_seed(
    cache_dir: Optional[Path] = None,
    seed_dir: Optional[Path] = None,
    log_dir: Optional[Path] = None,
) -> dict[str, str]:
    """同梱分を取り込む。戻り値は {service: 結果}。例外は出さない（キャンセルだけは出す）。"""
    cache_dir = Path(cache_dir) if cache_dir is not None else BASE_DIR / "cache"
    seed_dir = Path(seed_dir) if seed_dir is not None else default_seed_dir()
    if log_dir is None:
        log_dir = BASE_DIR / "logs"
    results: dict[str, str] = {}
    if not seed_dir.is_dir():
        if getattr(sys, "frozen", False):
            _log(f"同梱分が見つかりません（{seed_dir}）", log_dir)
        return results  # 開発環境など、同梱分が無いときは何もしない
    try:
        seed_traversal = _read_json_dict(seed_dir / TRAVERSAL_FILE_NAME)
    except Exception as exc:  # noqa: BLE001
        _log(f"同梱分の {TRAVERSAL_FILE_NAME} を読めないので取り込みません（{exc}）", log_dir)
        return {service: "seed_broken" for service in LIST_FILE_NAMES}
    for service in LIST_FILE_NAMES:
        try:
            with _IMPORT_LOCK:
                results[service] = _import_service(service, seed_dir, cache_dir, seed_traversal, log_dir)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, RuntimeError) and str(exc) == "SNAPSHOT_CANCELLED":
                raise
            _log(f"{service}: 取り込みに失敗しました。今までどおり取得します（{exc}）", log_dir)
            results[service] = "failed"
    return results
