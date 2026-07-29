"""API 負荷軽減まわり（一括取得・レート制御・User-Agent）のテスト。"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import cast
from unittest import mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from mybeatsaberstats import beatsaver_cache as bsc  # noqa: E402
from mybeatsaberstats import http_client as hc  # noqa: E402


def _map_payload(song_hash: str, key: str) -> dict:
    return {
        "id": key,
        "name": f"song-{key}",
        "description": "desc",
        "createdAt": "2026-01-01T00:00:00Z",
        "metadata": {"songName": f"song-{key}", "songAuthorName": "author", "levelAuthorName": "mapper"},
        "stats": {"score": 0.9, "upvotes": 10, "downvotes": 1},
        "versions": [{"hash": song_hash, "key": key, "coverURL": "http://c", "previewURL": "http://p"}],
        "uploader": {"name": "mapper", "verifiedMapper": True},
    }


class _FakeResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers: dict = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"status={self.status_code}")

    def json(self) -> object:
        return self._payload

    def close(self) -> None:
        return None


def test_fetch_by_hashes_parses_multi_hash_dict_response() -> None:
    """複数ハッシュ指定時の {hash: map} 形式を解釈できる。"""
    hashes = ["AAA111", "BBB222"]
    payload = {h.lower(): _map_payload(h, f"key{i}") for i, h in enumerate(hashes)}

    class _S:
        def get(self, url, timeout=None):  # noqa: ANN001, ANN202
            return _FakeResponse(payload)

    result = bsc._fetch_beatsaver_maps_by_hashes(cast(requests.Session, _S()), hashes)
    assert set(result) == {"AAA111", "BBB222"}
    assert result["AAA111"]["beatsaver_key"] == "key0"


def test_fetch_by_hashes_parses_single_map_response() -> None:
    """単一ハッシュ指定時の map オブジェクト形式も従来通り解釈できる。"""

    class _S:
        def get(self, url, timeout=None):  # noqa: ANN001, ANN202
            return _FakeResponse(_map_payload("AAA111", "key0"))

    result = bsc._fetch_beatsaver_maps_by_hashes(cast(requests.Session, _S()), ["AAA111"])
    assert result["AAA111"]["beatsaver_key"] == "key0"


def test_update_cache_batches_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """120 ハッシュが 1 件ずつではなく 50 件単位でまとめて取得される。"""
    monkeypatch.setattr(bsc, "_CACHE_PATH", tmp_path / "beatsaver_map_details.json")
    hashes = [f"{i:040X}" for i in range(120)]
    requested: list[list[str]] = []

    def _fake_fetch(session, batch):  # noqa: ANN001, ANN202
        requested.append(list(batch))
        # 実際の取得関数と同じく、meta 形式へ変換したものを返す
        return {
            h: cast(dict, bsc._meta_from_map_payload(_map_payload(h, f"key{h}")))
            for h in batch
        }

    monkeypatch.setattr(bsc, "_fetch_beatsaver_maps_by_hashes", _fake_fetch)

    cache = bsc.update_beatsaver_meta_cache(hashes, session=cast(requests.Session, object()))

    assert [len(b) for b in requested] == [50, 50, 20]
    assert len(cache) == 120
    # 2 回目は全件キャッシュ済みなのでリクエストが発生しない
    requested.clear()
    bsc.update_beatsaver_meta_cache(hashes, session=cast(requests.Session, object()))
    assert requested == []


def test_fetch_by_hashes_keys_by_requested_hash() -> None:
    """譜面が更新され versions の hash が違っても、要求した hash で引ける。

    meta 側の hash をキーにすると毎回「未取得」と判定され、
    同じハッシュを永久に取りに行ってしまう。
    """
    requested = "OLDHASH0000000000000000000000000000000A"
    payload = {requested.lower(): _map_payload("NEWHASH0000000000000000000000000000000B", "key0")}

    class _S:
        def get(self, url, timeout=None):  # noqa: ANN001, ANN202
            return _FakeResponse(payload)

    result = bsc._fetch_beatsaver_maps_by_hashes(cast(requests.Session, _S()), [requested])
    assert requested in result
    assert result[requested]["hash"] == requested
    assert result[requested]["beatsaver_key"] == "key0"


def test_updated_map_is_not_refetched_next_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """versions の hash が異なる譜面でも、2 回目はキャッシュ済みとして再取得しない。"""
    monkeypatch.setattr(bsc, "_CACHE_PATH", tmp_path / "beatsaver_map_details.json")
    requested = "OLDHASH0000000000000000000000000000000A"
    calls: list[int] = []

    def _fake_fetch(session, batch):  # noqa: ANN001, ANN202
        calls.append(len(batch))
        return {
            h: cast(dict, bsc._meta_from_map_payload(_map_payload("DIFFERENTHASH", "key0"), fallback_hash=h)) | {"hash": h}
            for h in batch
        }

    monkeypatch.setattr(bsc, "_fetch_beatsaver_maps_by_hashes", _fake_fetch)

    bsc.update_beatsaver_meta_cache([requested], session=cast(requests.Session, object()))
    assert calls == [1]
    bsc.update_beatsaver_meta_cache([requested], session=cast(requests.Session, object()))
    assert calls == [1], "2 回目で再取得が発生している"


def test_bl_leaderboard_disk_cache_avoids_refetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BeatLeader leaderboard は一度取得したらプロセスをまたいで再取得しない。"""
    from mybeatsaberstats.playlist import playlist_maps as pm

    monkeypatch.setattr(pm, "_BL_LEADERBOARD_CACHE_PATH", tmp_path / "bl_lb.json")
    monkeypatch.setattr(pm, "_BL_LEADERBOARD_MEMO", None)

    song_hash = "AAA111"
    docs = [{"versions": [{"hash": song_hash, "key": "k1"}]}]
    fetched_hashes: list[str] = []

    def _fake_fetch(session, h):  # noqa: ANN001, ANN202
        fetched_hashes.append(h)
        return {("Standard", "ExpertPlus"): "lb-1"}

    monkeypatch.setattr(pm, "_fetch_bl_leaderboards_by_hash", _fake_fetch)

    cache: dict = {}
    pm._prefetch_bl_leaderboards_for_docs(cast(requests.Session, object()), docs, cache)
    assert fetched_hashes == [song_hash]
    assert cache[song_hash] == {("Standard", "ExpertPlus"): "lb-1"}

    # プロセス再起動を模して、メモリキャッシュだけ捨てる
    monkeypatch.setattr(pm, "_BL_LEADERBOARD_MEMO", None)
    cache2: dict = {}
    pm._prefetch_bl_leaderboards_for_docs(cast(requests.Session, object()), docs, cache2)
    assert fetched_hashes == [song_hash], "ディスクキャッシュがあるのに再取得している"
    assert cache2[song_hash] == {("Standard", "ExpertPlus"): "lb-1"}


def test_session_sets_user_agent() -> None:
    """全リクエストにアプリ名入りの User-Agent が付く。"""
    session = hc.make_session()
    ua = session.headers["User-Agent"]
    assert ua.startswith("MyBeatSaberStats/")
    assert "github.com" in ua


def test_scoresaber_requests_are_serialized() -> None:
    """ScoreSaber 宛は同時実行されず、最小間隔が守られる。"""
    timestamps: list[float] = []
    lock = threading.Lock()

    def _fake_request(self, method, url, **kwargs):  # noqa: ANN001, ANN202
        with lock:
            timestamps.append(time.monotonic())
        return _FakeResponse({})

    with mock.patch.object(requests.Session, "request", _fake_request):
        session = hc.make_session()
        threads = [
            threading.Thread(target=lambda: session.get("https://scoresaber.com/api/players"))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    timestamps.sort()
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:])]
    policy = hc._policy_for("https://scoresaber.com/api/players")
    assert all(gap >= policy.min_interval * 0.9 for gap in gaps), gaps


def test_429_is_retried_following_retry_after() -> None:
    """429 は Retry-After に従って待機した上で再試行される。"""
    responses = [_FakeResponse({}, 429), _FakeResponse({}, 200)]
    responses[0].headers = {"Retry-After": "1"}

    def _fake_request(self, method, url, **kwargs):  # noqa: ANN001, ANN202
        return responses.pop(0)

    with mock.patch.object(requests.Session, "request", _fake_request):
        session = hc.make_session()
        started = time.monotonic()
        resp = session.get("https://api.accsaberreloaded.com/v1/maps")

    assert resp.status_code == 200
    assert time.monotonic() - started >= 1.0
