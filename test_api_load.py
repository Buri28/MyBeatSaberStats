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


class _FakeAccSaberSession:
    """AccSaber API を模したセッション。リクエスト内容を記録する。"""

    def __init__(self, player_id: str, name: str, leaderboard_pages: int = 20) -> None:
        self.player_id = player_id
        self.name = name
        self.leaderboard_pages = leaderboard_pages
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):  # noqa: ANN001, ANN202
        params = params or {}
        self.requests.append((url, dict(params)))

        if "/users/" in url:
            return _FakeResponse({"id": self.player_id, "name": self.name, "country": "JP"})

        entry = {
            "userId": self.player_id,
            "userName": self.name,
            "country": "JP",
            "ap": 100.5,
            "averageAcc": 0.98,
            "rankedPlays": 42,
            "ranking": 3800,
            "countryRanking": 28,
            "totalXp": 370218.4,
            "level": 80,
        }
        if params.get("search"):
            # 名前で絞ると 1 ページに収まる
            return _FakeResponse({"content": [entry], "last": True})

        # 全走査: 最終ページにだけ本人がいる
        page = int(params.get("page", 0))
        is_last = page >= self.leaderboard_pages - 1
        content = [entry] if is_last else [{"userId": "other", "ranking": page}]
        return _FakeResponse({"content": content, "last": is_last})


def test_accsaber_uses_search_instead_of_full_scan() -> None:
    """カテゴリ順位が search 1 回で取れ、全走査しない。"""
    import mybeatsaberstats.accsaber_reloaded as acc

    pid = "76561198324870685"
    session = _FakeAccSaberSession(pid, "Buri")
    result = acc.fetch_player_all_categories(pid, country="JP", session=cast(requests.Session, session))

    assert all(v is not None for v in result.values())
    assert result["overall"].ap == 100.5
    assert result["overall"].rank_global == 3800
    # プロフィール 1 回 + 4 カテゴリ = 5 リクエスト（全走査なら 4×20=80）
    assert len(session.requests) == 5, session.requests
    assert all("search" in p for u, p in session.requests if "/leaderboards/" in u)


def test_accsaber_falls_back_to_full_scan_when_search_misses() -> None:
    """search で本人が見つからない場合は従来の全走査に落ちる。"""
    import mybeatsaberstats.accsaber_reloaded as acc

    pid = "76561198324870685"
    session = _FakeAccSaberSession(pid, "Buri", leaderboard_pages=5)

    # search では別人しか返さないようにする
    original_get = session.get

    def _get(url, params=None, timeout=None):  # noqa: ANN001, ANN202
        params = params or {}
        if params.get("search"):
            session.requests.append((url, dict(params)))
            return _FakeResponse({"content": [{"userId": "someone-else"}], "last": True})
        return original_get(url, params, timeout)

    session.get = _get  # type: ignore[assignment]

    player = acc._search_in_leaderboard(
        acc.CATEGORY_IDS["overall"], pid, "JP", cast(requests.Session, session), player_name="Buri"
    )
    assert player is not None, "フォールバックが働いていない"
    assert player.ap == 100.5
    scan_requests = [p for u, p in session.requests if "search" not in p and "/leaderboards/" in u]
    assert len(scan_requests) == 5


def test_accsaber_profile_is_fetched_once_per_session() -> None:
    """/users/{id} が同一セッションで何度も叩かれない。"""
    import mybeatsaberstats.accsaber_reloaded as acc

    pid = "76561198324870685"
    session = _FakeAccSaberSession(pid, "Buri")
    s = cast(requests.Session, session)

    acc.fetch_player_all_categories(pid, country="JP", session=s)
    acc.fetch_player_xp(pid, country="JP", session=s)
    acc.fetch_player_level_title(pid, session=s)

    user_calls = [u for u, _ in session.requests if "/users/" in u]
    assert len(user_calls) == 1, user_calls


def test_map_by_code_is_not_fetched_twice() -> None:
    """/v1/maps が 500 のとき、同じ by-code を 2 系統から二重に取りに行かない。"""
    import mybeatsaberstats.accsaber_reloaded as acc

    calls: list[str] = []

    class _S:
        def get(self, url, timeout=None):  # noqa: ANN001, ANN202
            calls.append(url)
            return _FakeResponse({"id": "m1", "beatsaverCode": "4f185", "songHash": "AAA"})

    s = cast(requests.Session, _S())
    first = acc._fetch_map_by_code("4f185", s)
    second = acc._fetch_map_by_code("4f185", s)

    assert first == second
    assert len(calls) == 1, calls


def test_difficulties_fallback_runs_once_per_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """difficulties フォールバックの全件クロールがセッション内で 1 回に畳まれる。"""
    import mybeatsaberstats.accsaber_reloaded as acc

    crawls = {"n": 0}

    def _fake_hashes(session):  # noqa: ANN001, ANN202
        crawls["n"] += 1
        return {}

    monkeypatch.setattr(acc, "_fetch_song_hashes_by_difficulty_id", _fake_hashes)
    monkeypatch.setattr(acc, "_backfill_missing_song_hashes", lambda songs, session: None)

    class _S:
        def get(self, url, params=None, timeout=None):  # noqa: ANN001, ANN202
            return _FakeResponse({
                "content": [{"mapId": "m1", "categoryId": "c", "beatsaverCode": "4f185"}],
                "last": True,
            })

    s = cast(requests.Session, _S())
    first = acc._fetch_all_maps_via_difficulties(s)
    second = acc._fetch_all_maps_via_difficulties(s)

    assert first == second
    assert crawls["n"] == 1, "全件クロールが 2 回走っている"


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


def test_request_scope_counts_per_host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """スコープを抜けるとホスト別のリクエスト数がログへ出る。"""
    monkeypatch.setattr(hc, "REQUEST_LOG_PATH", tmp_path / "api_requests.log")
    hc.reset_request_stats()

    urls = [
        "https://scoresaber.com/api/players",
        "https://scoresaber.com/api/players",
        "https://api.beatleader.xyz/player/1",
    ]

    def _fake_request(self, method, url, **kwargs):  # noqa: ANN001, ANN202
        return _FakeResponse({})

    with mock.patch.object(requests.Session, "request", _fake_request):
        session = hc.make_session()
        with hc.request_scope("テスト処理"):
            for u in urls:
                session.get(u)

    logged = (tmp_path / "api_requests.log").read_text(encoding="utf-8")
    assert "テスト処理: 合計 3 リクエスト" in logged
    assert "scoresaber.com: 2 回" in logged
    assert "api.beatleader.xyz: 1 回" in logged
    # 多く叩いた API が先に並ぶ
    assert logged.index("scoresaber.com") < logged.index("api.beatleader.xyz")
    # エンドポイント内訳が出る（ID はプレースホルダに正規化される）
    assert "GET /api/players: 2 回" in logged
    assert "GET /player/{id}: 1 回" in logged


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.accsaberreloaded.com/v1/users/76561198324870685", "GET /v1/users/{id}"),
        ("https://api.accsaberreloaded.com/v1/users/76561198324870685/milestones?page=3",
         "GET /v1/users/{id}/milestones"),
        ("https://api.accsaberreloaded.com/v1/leaderboards/b0000000-0000-0000-0000-000000000005",
         "GET /v1/leaderboards/{uuid}"),
        ("https://api.beatleader.xyz/leaderboards/hash/" + "A" * 40,
         "GET /leaderboards/hash/{hash}"),
        ("https://beatsaver.com/api/maps/hash/" + "A" * 40 + "," + "B" * 40,
         "GET /api/maps/hash/{hashes}"),
        ("https://scoresaber.com/api/player/76561198324870685/scores",
         "GET /api/player/{id}/scores"),
        ("https://scoresaber.com/api/leaderboards", "GET /api/leaderboards"),
        # BeatSaver の一括 ID 指定はログが長くなるので必ず潰す
        ("https://beatsaver.com/api/maps/ids/52697,4c05f,52c50", "GET /api/maps/ids/{ids}"),
        ("https://beatsaver.com/api/maps/ids/52697", "GET /api/maps/ids/{ids}"),
        ("https://api.accsaberreloaded.com/v1/maps/by-code/52697", "GET /v1/maps/by-code/{code}"),
    ],
)
def test_endpoint_normalization(url: str, expected: str) -> None:
    """URL の可変部分がプレースホルダへ正規化される。"""
    assert hc._endpoint_of("GET", url) == expected


def test_request_scope_reports_errors_and_throttling(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """404 / 500 / 429 がそれぞれ区別して記録される。"""
    monkeypatch.setattr(hc, "REQUEST_LOG_PATH", tmp_path / "api_requests.log")
    hc.reset_request_stats()

    statuses = [404, 500, 200]

    def _fake_request(self, method, url, **kwargs):  # noqa: ANN001, ANN202
        return _FakeResponse({}, statuses.pop(0))

    with mock.patch.object(requests.Session, "request", _fake_request):
        session = hc.make_session()
        with hc.request_scope("エラー混在"):
            for _ in range(3):
                session.get("https://beatsaver.com/api/maps/hash/x")

    logged = (tmp_path / "api_requests.log").read_text(encoding="utf-8")
    assert "合計 3 リクエスト" in logged
    assert "エラー 1 回" in logged
    assert "404 1 回" in logged


def test_request_scope_logs_even_on_exception(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """処理が例外で落ちてもサマリは出力される。"""
    monkeypatch.setattr(hc, "REQUEST_LOG_PATH", tmp_path / "api_requests.log")
    hc.reset_request_stats()

    def _fake_request(self, method, url, **kwargs):  # noqa: ANN001, ANN202
        return _FakeResponse({})

    with mock.patch.object(requests.Session, "request", _fake_request):
        session = hc.make_session()
        with pytest.raises(ValueError):
            with hc.request_scope("途中で失敗"):
                session.get("https://scoresaber.com/api/players")
                raise ValueError("boom")

    logged = (tmp_path / "api_requests.log").read_text(encoding="utf-8")
    assert "途中で失敗: 合計 1 リクエスト" in logged


def test_nested_request_scopes_do_not_double_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """入れ子のスコープでは内側の分が外側から二重に引かれない。"""
    monkeypatch.setattr(hc, "REQUEST_LOG_PATH", tmp_path / "api_requests.log")
    hc.reset_request_stats()

    def _fake_request(self, method, url, **kwargs):  # noqa: ANN001, ANN202
        return _FakeResponse({})

    with mock.patch.object(requests.Session, "request", _fake_request):
        session = hc.make_session()
        with hc.request_scope("外側"):
            session.get("https://scoresaber.com/api/players")
            with hc.request_scope("内側"):
                session.get("https://scoresaber.com/api/players")
                session.get("https://scoresaber.com/api/players")

    logged = (tmp_path / "api_requests.log").read_text(encoding="utf-8")
    assert "内側: 合計 2 リクエスト" in logged
    # 外側は内側を含む全体の 3 件
    assert "外側: 合計 3 リクエスト" in logged


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
