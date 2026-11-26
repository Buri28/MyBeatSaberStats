"""AccSaber 全マップキャッシュの取り直しの確認（実 API は呼ばない。応答はすべてテスト用の偽物）。

- batch の取り込みで fetched_at を進めない（中身が変わっても）
- by-code の一部失敗では batches_merged_at を進めず、次の回に読み直す
- totalElements に届かない全件取得では fetched_at を進めない
- 未完了・失敗の後は待ち時間の間、全件取得を繰り返さない（歯止め）
- 保存済みキャッシュのコピーで、分母（カテゴリごとの譜面数）が変わらない・正しく増える
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pytest
import requests

import mybeatsaberstats.accsaber_reloaded as mod

REAL_CACHE = Path(__file__).resolve().parent.parent / "cache" / "accsaber_reloaded_maps.json"

STD = mod.CATEGORY_IDS["standard"]
TRUE = mod.CATEGORY_IDS["true"]
TECH = mod.CATEGORY_IDS["tech"]


def _z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _diff(diff_id: str, map_id: str, cat: str, status: str = "RANKED", code: Optional[str] = None) -> Dict:
    return {
        "id": diff_id,
        "mapId": map_id,
        "categoryId": cat,
        "status": status,
        "difficulty": "EXPERT",
        "characteristic": "Standard",
        "beatsaverCode": code or map_id,
        "songName": map_id,
    }


def _song(map_id: str, diffs: List[Dict]) -> Dict:
    return {"id": map_id, "beatsaverCode": map_id, "songHash": f"H{map_id}", "songName": map_id, "difficulties": diffs}


class _Resp:
    def __init__(self, payload, status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            err = requests.HTTPError(f"HTTP {self.status_code}")
            err.response = self  # type: ignore[assignment]
            raise err

    def json(self):
        return self._payload


class _FakeSession:
    """URL ごとの応答を返す。呼ばれた URL は calls に残す。"""

    def __init__(
        self,
        *,
        batches: Optional[List[Dict]] = None,
        by_code: Optional[Dict[str, object]] = None,
        difficulties: Optional[List[Dict]] = None,
        total_elements: Optional[int] = None,
        page_size: int = 200,
        always_not_last: bool = False,
        fail_difficulties: bool = False,
    ) -> None:
        self.batches = batches or []
        self.by_code = by_code or {}
        self.difficulties = difficulties or []
        self.total_elements = total_elements if total_elements is not None else len(self.difficulties)
        self.page_size = page_size
        self.always_not_last = always_not_last
        self.fail_difficulties = fail_difficulties
        self.calls: List[str] = []

    def get(self, url, params=None, timeout=None):  # noqa: ANN001, ANN201
        self.calls.append(url)
        if url.endswith("/batches"):
            return _Resp({"content": self.batches, "last": True})
        if "/maps/by-code/" in url:
            code = url.rsplit("/", 1)[-1]
            value = self.by_code.get(code)
            if isinstance(value, int):
                return _Resp({}, status=value)
            return _Resp(value)
        if url.endswith("/maps/difficulties/all"):
            if self.fail_difficulties:
                return _Resp({}, status=500)
            return _Resp([{"id": d["id"], "songHash": f"H{d['mapId']}"} for d in self.difficulties])
        if url.endswith("/maps/difficulties"):
            if self.fail_difficulties:
                return _Resp({}, status=500)
            page = int((params or {}).get("page", 0))
            start = page * self.page_size
            content = self.difficulties[start:start + self.page_size]
            total_pages = max(1, -(-len(self.difficulties) // self.page_size))
            last = (start + self.page_size >= len(self.difficulties)) and not self.always_not_last
            return _Resp({
                "content": content,
                "totalElements": self.total_elements,
                "totalPages": total_pages,
                "last": last,
            })
        raise AssertionError(f"unexpected url {url}")


@pytest.fixture
def iso(monkeypatch, tmp_path):
    """キャッシュの置き場所を一時フォルダへ移し、モジュールの状態を初期化する。"""
    monkeypatch.setattr(mod, "_ALL_MAPS_CACHE_FILE", tmp_path / "maps.json")
    monkeypatch.setattr(mod, "_MAP_COUNTS_CACHE_FILE", tmp_path / "counts.json")
    monkeypatch.setattr(mod, "_FULL_FETCH_STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(mod, "_SONG_HASH_CACHE_FILE", tmp_path / "song_hash.json")
    monkeypatch.setattr(mod, "_recent_batch_checked_at", 0.0)
    # songHash の補完は BeatSaver を呼ぶので止める（このテストの対象外）
    monkeypatch.setattr(mod, "_backfill_missing_song_hashes", lambda songs, session: None)
    return tmp_path


def _write_cache(path: Path, maps: List[Dict], fetched_at: str, merged_at: Optional[str] = None) -> None:
    data: Dict = {"fetched_at": fetched_at, "maps": maps}
    if merged_at:
        data["batches_merged_at"] = merged_at
    path.write_text(json.dumps(data), encoding="utf-8")


def _read(path: Path) -> Dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _batch(released: datetime, codes: List[str]) -> Dict:
    return {"releasedAt": _z(released), "difficulties": [{"beatsaverCode": c} for c in codes]}


# ── B-1：batch の取り込み ─────────────────────────────────────────────


def test_batch_merge_with_changes_keeps_fetched_at(iso) -> None:
    now = datetime.now(timezone.utc)
    fetched_at = _z(now - timedelta(hours=5))
    _write_cache(mod._ALL_MAPS_CACHE_FILE, [_song("old", [_diff("d-old", "old", TRUE)])], fetched_at)

    session = _FakeSession(
        batches=[_batch(now - timedelta(hours=1), ["new"])],
        by_code={"new": _song("new", [_diff("d-new", "new", STD)])},
    )
    maps = mod._merge_all_maps_with_recent_batches(session)  # type: ignore[arg-type]

    assert maps is not None and len(maps) == 2
    saved = _read(mod._ALL_MAPS_CACHE_FILE)
    assert saved["fetched_at"] == fetched_at  # 中身が変わっても進めない
    merged_at = mod._parse_utc_timestamp(saved.get("batches_merged_at"))
    assert merged_at is not None and merged_at > now - timedelta(minutes=1)
    assert mod.get_reloaded_map_counts_from_cache() == {"true": 1, "standard": 1, "overall": 2}
    # 24 時間の全件取得の判定も、全件取得の時刻のまま
    assert mod._is_all_maps_cache_fresh() is True


def test_batch_merge_partial_failure_keeps_merged_at_and_retries(iso, monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    fetched_at = _z(now - timedelta(hours=5))
    prev_merged = _z(now - timedelta(hours=3))
    _write_cache(mod._ALL_MAPS_CACHE_FILE, [_song("old", [_diff("d-old", "old", TRUE)])], fetched_at, prev_merged)
    batches = [_batch(now - timedelta(hours=1), ["ok1", "bad1"])]

    # 1 回目：ok1 は取れ、bad1 は 503
    s1 = _FakeSession(batches=batches, by_code={"ok1": _song("ok1", [_diff("d-ok1", "ok1", STD)]), "bad1": 503})
    mod._merge_all_maps_with_recent_batches(s1)  # type: ignore[arg-type]

    saved = _read(mod._ALL_MAPS_CACHE_FILE)
    assert saved["fetched_at"] == fetched_at
    assert saved.get("batches_merged_at") == prev_merged  # 進めない
    assert {m["id"] for m in saved["maps"]} == {"old", "ok1"}  # 取れた分は保存する

    # 2 回目（10 分の間隔の後）：同じ batch を読み直し、bad1 を取り直す
    monkeypatch.setattr(mod, "_recent_batch_checked_at", 0.0)
    s2 = _FakeSession(
        batches=batches,
        by_code={"ok1": _song("ok1", [_diff("d-ok1", "ok1", STD)]), "bad1": _song("bad1", [_diff("d-bad1", "bad1", TECH)])},
    )
    mod._merge_all_maps_with_recent_batches(s2)  # type: ignore[arg-type]

    assert any(url.endswith("/maps/by-code/bad1") for url in s2.calls)
    saved = _read(mod._ALL_MAPS_CACHE_FILE)
    assert {m["id"] for m in saved["maps"]} == {"old", "ok1", "bad1"}
    assert saved["fetched_at"] == fetched_at
    assert saved.get("batches_merged_at") != prev_merged  # 全部取れたので進む
    assert mod.get_reloaded_map_counts_from_cache() == {"true": 1, "standard": 1, "tech": 1, "overall": 3}


def test_batch_merge_non_dict_response_counts_as_failure(iso) -> None:
    now = datetime.now(timezone.utc)
    fetched_at = _z(now - timedelta(hours=5))
    _write_cache(mod._ALL_MAPS_CACHE_FILE, [_song("old", [_diff("d-old", "old", TRUE)])], fetched_at)
    s1 = _FakeSession(batches=[_batch(now - timedelta(hours=1), ["x"])], by_code={"x": None})
    mod._merge_all_maps_with_recent_batches(s1)  # type: ignore[arg-type]
    saved = _read(mod._ALL_MAPS_CACHE_FILE)
    assert "batches_merged_at" not in saved
    assert saved["fetched_at"] == fetched_at


# ── B-2：全件取得の totalElements の確認 ─────────────────────────────


def _fresh_diffs(n_std: int, n_tech: int, pending: int = 0) -> List[Dict]:
    diffs = [_diff(f"s{i}", f"ms{i}", STD) for i in range(n_std)]
    diffs += [_diff(f"t{i}", f"mt{i}", TECH) for i in range(n_tech)]
    diffs += [_diff(f"p{i}", f"mp{i}", STD, status="QUEUE") for i in range(pending)]
    return diffs


def test_full_fetch_counts_all_statuses_against_total(iso) -> None:
    # QUEUE も totalElements に入る（status で絞っていない）ので、全部数えて完了になる
    diffs = _fresh_diffs(3, 2, pending=2)
    session = _FakeSession(difficulties=diffs, total_elements=7, page_size=3)
    result = mod._fetch_all_maps_via_difficulties(session)  # type: ignore[arg-type]
    assert result.read_count == 7 and result.expected_total == 7
    assert mod._is_full_fetch_complete(result) is True


def test_full_fetch_short_of_total_keeps_fetched_at(iso) -> None:
    now = datetime.now(timezone.utc)
    old_fetched = _z(now - timedelta(hours=30))  # 期限切れ → 全件取得に行く
    old_maps = [_song("ms0", [_diff("s0", "ms0", STD)]), _song("gone", [_diff("g0", "gone", TRUE)])]
    _write_cache(mod._ALL_MAPS_CACHE_FILE, old_maps, old_fetched)

    # API の総数は 10 だが、実際には 6 件しか返らない（途中で止まった）
    diffs = _fresh_diffs(4, 2)
    session = _FakeSession(difficulties=diffs, total_elements=10, page_size=3)
    mod.fetch_and_save_all_maps_cache(session=session)  # type: ignore[arg-type]

    saved = _read(mod._ALL_MAPS_CACHE_FILE)
    assert saved["fetched_at"] == old_fetched  # 完了扱いにしない
    ids = {m["id"] for m in saved["maps"]}
    assert "gone" in ids  # 読めなかった譜面は消さない
    assert {"ms0", "ms3", "mt1"} <= ids  # 新しく読めた分は足す
    state = _read(mod._FULL_FETCH_STATE_FILE)
    assert state["status"] == "incomplete" and state["expected_total"] == 10 and state["read_count"] == 6
    assert mod._is_all_maps_cache_fresh() is False


def test_full_fetch_guard_stop_is_incomplete(iso, monkeypatch) -> None:
    monkeypatch.setattr(mod, "_MAX_PAGES_GUARD", 1)
    diffs = _fresh_diffs(9, 0)
    # last が常に false（API 異常）で、ガードに当たって止まる
    session = _FakeSession(difficulties=diffs, total_elements=9, page_size=3, always_not_last=True)
    result = mod._fetch_all_maps_via_difficulties(session)  # type: ignore[arg-type]
    assert result.reached_last is False
    assert mod._is_full_fetch_complete(result) is False


def test_full_fetch_complete_advances_fetched_at_and_clears_state(iso) -> None:
    now = datetime.now(timezone.utc)
    old_fetched = _z(now - timedelta(hours=30))
    _write_cache(mod._ALL_MAPS_CACHE_FILE, [_song("gone", [_diff("g0", "gone", TRUE)])], old_fetched)
    mod._record_full_fetch_result("incomplete", 10, 6)
    # 待ち時間を過ぎた
    state = _read(mod._FULL_FETCH_STATE_FILE)
    state["attempted_at"] = _z(now - timedelta(hours=25))
    mod._FULL_FETCH_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")

    session = _FakeSession(difficulties=_fresh_diffs(2, 1), page_size=2)
    mod.fetch_and_save_all_maps_cache(session=session)  # type: ignore[arg-type]

    saved = _read(mod._ALL_MAPS_CACHE_FILE)
    assert mod._parse_utc_timestamp(saved["fetched_at"]) > now - timedelta(minutes=1)
    assert {m["id"] for m in saved["maps"]} == {"ms0", "ms1", "mt0"}  # 完了したときは置き換え（外れた譜面は消える）
    assert mod._load_full_fetch_state() == {}
    assert mod.get_reloaded_map_counts_from_cache() == {"standard": 2, "tech": 1, "overall": 3}


# ── 歯止め ───────────────────────────────────────────────────────


def _count_full_fetch_calls(session: _FakeSession) -> int:
    return sum(1 for url in session.calls if url.endswith("/maps/difficulties"))


def test_backoff_after_incomplete_fetch(iso) -> None:
    now = datetime.now(timezone.utc)
    old_fetched = _z(now - timedelta(hours=30))
    _write_cache(mod._ALL_MAPS_CACHE_FILE, [_song("ms0", [_diff("s0", "ms0", STD)])], old_fetched)

    s1 = _FakeSession(difficulties=_fresh_diffs(3, 0), total_elements=5, page_size=2)
    mod.fetch_and_save_all_maps_cache(session=s1)  # type: ignore[arg-type]
    assert _count_full_fetch_calls(s1) > 0

    # 直後の Snapshot：全件取得はせず batch の確認だけ
    s2 = _FakeSession(difficulties=_fresh_diffs(3, 0), total_elements=5, page_size=2)
    mod._recent_batch_checked_at = 0.0
    mod.fetch_and_save_all_maps_cache(session=s2)  # type: ignore[arg-type]
    assert _count_full_fetch_calls(s2) == 0
    assert any(url.endswith("/batches") for url in s2.calls)

    # 23 時間後もまだ待つ
    state = _read(mod._FULL_FETCH_STATE_FILE)
    state["attempted_at"] = _z(now - timedelta(hours=23))
    mod._FULL_FETCH_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    s3 = _FakeSession(difficulties=_fresh_diffs(3, 0), total_elements=5, page_size=2)
    mod._recent_batch_checked_at = 0.0
    mod.fetch_and_save_all_maps_cache(session=s3)  # type: ignore[arg-type]
    assert _count_full_fetch_calls(s3) == 0

    # 24 時間を過ぎたら、もう一度全件を取る
    state["attempted_at"] = _z(now - timedelta(hours=24, minutes=1))
    mod._FULL_FETCH_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    s4 = _FakeSession(difficulties=_fresh_diffs(3, 0), page_size=2)
    mod.fetch_and_save_all_maps_cache(session=s4)  # type: ignore[arg-type]
    assert _count_full_fetch_calls(s4) > 0
    assert mod._load_full_fetch_state() == {}


def test_backoff_after_failed_fetch(iso) -> None:
    now = datetime.now(timezone.utc)
    old_fetched = _z(now - timedelta(hours=30))
    _write_cache(mod._ALL_MAPS_CACHE_FILE, [_song("ms0", [_diff("s0", "ms0", STD)])], old_fetched)

    s1 = _FakeSession(fail_difficulties=True)
    mod.fetch_and_save_all_maps_cache(session=s1)  # type: ignore[arg-type]
    assert _read(mod._FULL_FETCH_STATE_FILE)["status"] == "failed"
    assert _read(mod._ALL_MAPS_CACHE_FILE)["fetched_at"] == old_fetched

    s2 = _FakeSession(difficulties=_fresh_diffs(1, 0))
    mod._recent_batch_checked_at = 0.0
    mod.fetch_and_save_all_maps_cache(session=s2)  # type: ignore[arg-type]
    assert not any("/maps/difficulties" in url for url in s2.calls)

    state = _read(mod._FULL_FETCH_STATE_FILE)
    state["attempted_at"] = _z(now - timedelta(hours=1, minutes=1))
    mod._FULL_FETCH_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    s3 = _FakeSession(difficulties=_fresh_diffs(1, 0))
    mod.fetch_and_save_all_maps_cache(session=s3)  # type: ignore[arg-type]
    assert _count_full_fetch_calls(s3) > 0


def test_map_counts_not_lowered_by_incomplete_fetch(iso) -> None:
    mod._MAP_COUNTS_CACHE_FILE.write_text(
        json.dumps({"fetched_at": _z(datetime.now(timezone.utc)), "standard": {"count": 50}}), encoding="utf-8"
    )
    session = _FakeSession(difficulties=_fresh_diffs(3, 0), total_elements=50, page_size=2)
    counts = mod.fetch_reloaded_map_counts(session=session)  # type: ignore[arg-type]
    assert counts.get("standard") == 50


# ── 保存済みキャッシュのコピーでの確認（分母） ───────────────────────


def _real_cache_copy(tmp: Path) -> Dict:
    if not REAL_CACHE.exists():
        pytest.skip("cache/accsaber_reloaded_maps.json が無い")
    dst = tmp / "real_copy.json"
    shutil.copyfile(REAL_CACHE, dst)  # 本物は読むだけ。書き換えるのはコピー
    return json.loads(dst.read_text(encoding="utf-8"))


def _all_diffs(payload: Dict) -> List[Dict]:
    return [dict(d) for m in payload["maps"] for d in m.get("difficulties", [])]


def test_real_cache_copy_denominator_unchanged_and_increases(iso) -> None:
    payload = _real_cache_copy(iso)
    expected = mod._count_non_pending_map_counts(payload["maps"])
    now = datetime.now(timezone.utc)

    # 期限切れのコピーを置く
    _write_cache(mod._ALL_MAPS_CACHE_FILE, payload["maps"], _z(now - timedelta(hours=30)))
    diffs = _all_diffs(payload)

    # (a) 同じ内容が全部返る全件取得：分母は変わらない
    s1 = _FakeSession(difficulties=diffs, page_size=200)
    mod.fetch_and_save_all_maps_cache(session=s1)  # type: ignore[arg-type]
    assert _count_full_fetch_calls(s1) == -(-len(diffs) // 200)
    assert mod._count_non_pending_map_counts(mod.load_all_maps_from_cache() or []) == expected
    assert mod.get_reloaded_map_counts_from_cache() == expected

    # (b) 新しい batch で standard 2・tech 1 がランク、pending 1 が来る：分母は 2 と 1 だけ増える
    mod._recent_batch_checked_at = 0.0
    new_song = _song("new-map", [
        _diff("nd1", "new-map", STD), _diff("nd2", "new-map", STD), _diff("nd3", "new-map", TECH),
        _diff("nd4", "new-map", TRUE, status="QUEUE"),
    ])
    s2 = _FakeSession(batches=[_batch(now + timedelta(minutes=1), ["new-map"])], by_code={"new-map": new_song})
    mod.fetch_and_save_all_maps_cache(session=s2)  # type: ignore[arg-type]
    after = mod.get_reloaded_map_counts_from_cache()
    assert after["standard"] == expected["standard"] + 2
    assert after["tech"] == expected["tech"] + 1
    assert after["true"] == expected["true"]
    assert after["overall"] == expected["overall"] + 3
    saved = _read(mod._ALL_MAPS_CACHE_FILE)
    assert mod._is_all_maps_cache_fresh() is True  # fetched_at は (a) の全件取得の時刻のまま

    # (c) 24 時間後、途中で止まる全件取得（最初の 2 ページだけ）：分母は下がらない、fetched_at も進まない
    saved["fetched_at"] = _z(now - timedelta(hours=25))
    mod._ALL_MAPS_CACHE_FILE.write_text(json.dumps(saved), encoding="utf-8")
    mod._recent_batch_checked_at = 0.0
    s3 = _FakeSession(difficulties=diffs[:400], total_elements=len(diffs) + 4, page_size=200)
    mod.fetch_and_save_all_maps_cache(session=s3)  # type: ignore[arg-type]
    assert mod.get_reloaded_map_counts_from_cache() == after
    assert _read(mod._ALL_MAPS_CACHE_FILE)["fetched_at"] == saved["fetched_at"]
    assert _read(mod._FULL_FETCH_STATE_FILE)["status"] == "incomplete"


# ── レビューで足した確認 ───────────────────────────────────────────


def test_incomplete_fetch_without_maps_cache_does_not_lower_counts(iso) -> None:
    # 全マップキャッシュが無く、総譜面数のファイルだけがある状態で、全件取得が途中で止まる
    mod._MAP_COUNTS_CACHE_FILE.write_text(
        json.dumps({"fetched_at": _z(datetime.now(timezone.utc)), "standard": {"count": 50}, "tech": {"count": 1}}),
        encoding="utf-8",
    )
    session = _FakeSession(difficulties=_fresh_diffs(3, 2), total_elements=60, page_size=2)
    mod.fetch_and_save_all_maps_cache(session=session)  # type: ignore[arg-type]

    saved = _read(mod._ALL_MAPS_CACHE_FILE)
    assert "fetched_at" not in saved  # 全件取得を一度も終えていない
    assert len(saved["maps"]) == 5  # 読めた分は残す
    counts = mod.get_reloaded_map_counts_from_cache()
    assert counts["standard"] == 50  # 下げない
    assert counts["tech"] == 2  # 増えた分は増やす
    assert _read(mod._FULL_FETCH_STATE_FILE)["status"] == "incomplete"


def test_batch_merge_stops_on_rate_limit(iso) -> None:
    now = datetime.now(timezone.utc)
    fetched_at = _z(now - timedelta(hours=5))
    _write_cache(mod._ALL_MAPS_CACHE_FILE, [_song("old", [_diff("d-old", "old", TRUE)])], fetched_at)
    session = _FakeSession(
        batches=[_batch(now - timedelta(hours=1), ["ok1", "limited", "later"])],
        by_code={"ok1": _song("ok1", [_diff("d-ok1", "ok1", STD)]), "limited": 429,
                 "later": _song("later", [_diff("d-later", "later", STD)])},
    )
    mod._merge_all_maps_with_recent_batches(session)  # type: ignore[arg-type]
    assert not any(url.endswith("/maps/by-code/later") for url in session.calls)  # 429 の後は叩かない
    saved = _read(mod._ALL_MAPS_CACHE_FILE)
    assert {m["id"] for m in saved["maps"]} == {"old", "ok1"}
    assert "batches_merged_at" not in saved and saved["fetched_at"] == fetched_at


def test_incomplete_result_is_not_reused_in_same_session(iso) -> None:
    session = _FakeSession(difficulties=_fresh_diffs(3, 0), total_elements=9, page_size=2)
    mod._fetch_all_maps_via_difficulties(session)  # type: ignore[arg-type]
    first = _count_full_fetch_calls(session)
    mod._fetch_all_maps_via_difficulties(session)  # type: ignore[arg-type]
    assert _count_full_fetch_calls(session) == first * 2

    complete = _FakeSession(difficulties=_fresh_diffs(3, 0), page_size=2)
    mod._fetch_all_maps_via_difficulties(complete)  # type: ignore[arg-type]
    n = _count_full_fetch_calls(complete)
    mod._fetch_all_maps_via_difficulties(complete)  # type: ignore[arg-type]
    assert _count_full_fetch_calls(complete) == n  # 完了した結果はセッション内で使い回す


# ── aitask #412：全件取得が途中で止まったときの曲の難易度 ─────────────────


def test_full_fetch_stopped_midway_does_not_save_partial_songs(iso) -> None:
    """難易度単位のページングが途中で止まっても、曲の難易度を一部だけにして保存しない。

    - 前回のキャッシュにある曲：読めた難易度だけ新しい内容にし、読めなかった難易度は前回のまま残す。
    - 前回のキャッシュに無い曲：止まった所のページの曲も、それより前のページの曲も保存しない。難易度の一覧は曲ごとに
      並んでいるとは限らず、止まった所より前のページで読めた曲も、続きの難易度が読めなかったページにあるかもしれない
      （aitask #413 のレビュー）。
    """
    now = datetime.now(timezone.utc)
    old_fetched = _z(now - timedelta(hours=30))  # 期限切れ → 全件取得に行く
    cached = _song("m1", [_diff("d1", "m1", STD), _diff("d2", "m1", TECH), _diff("d3", "m1", TRUE)])
    _write_cache(mod._ALL_MAPS_CACHE_FILE, [cached], old_fetched)

    d1_new = dict(_diff("d1", "m1", STD), complexity=9.9)
    d2_new = dict(_diff("d2", "m1", TECH), complexity=8.8)
    # page 0: [m1.d1, q.q1] / page 1: [m1.d2, n.n1] / page 2: 空（last=false のまま）→ 途中で止まる
    # （m1.d3 と n.n2 は読めていない）
    diffs = [d1_new, _diff("q1", "q", STD), d2_new, _diff("n1", "n", STD)]
    session = _FakeSession(difficulties=diffs, total_elements=6, page_size=2, always_not_last=True)
    mod.fetch_and_save_all_maps_cache(session=session)  # type: ignore[arg-type]

    saved = _read(mod._ALL_MAPS_CACHE_FILE)
    assert saved["fetched_at"] == old_fetched  # 完了扱いにしない（今までどおり）
    songs = {m["id"]: m for m in saved["maps"]}
    m1 = {d["id"]: d for d in songs["m1"]["difficulties"]}
    assert set(m1) == {"d1", "d2", "d3"}  # 読めなかった d3 を落とさない
    assert m1["d1"].get("complexity") == 9.9 and m1["d2"].get("complexity") == 8.8  # 読めた分は新しい内容
    assert "q" not in songs  # 止まった所より前で読めた新しい曲も、難易度が揃っているか分からないので保存しない
    assert "n" not in songs  # 止まった所の新しい曲は、難易度が揃っていないかもしれないので保存しない
    assert _read(mod._FULL_FETCH_STATE_FILE)["status"] == "incomplete"
