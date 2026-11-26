"""get_memoized：1 回の Snapshot（＝同じセッション）の中では、時間に関係なく同じ URL を 1 回しか取らない。"""
import sys
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats import http_client  # noqa: E402
from mybeatsaberstats import beatleader as bl_basic  # noqa: E402
from mybeatsaberstats.collector import beatleader as bl_collector  # noqa: E402
from mybeatsaberstats.collector import collector  # noqa: E402
from mybeatsaberstats.collector import scoresaber as ss_collector  # noqa: E402

URL = "https://api.beatleader.com/player/1"


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.body = body
        self.url = URL

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSession(requests.Session):
    """外へは通信しない。get の回数と URL を記録し、決めた応答を順に返す。"""

    def __init__(self, statuses=(200,)):
        super().__init__()
        self.calls = []
        self._statuses = list(statuses)

    def get(self, url, **kwargs):  # noqa: D401
        self.calls.append(url)
        status = self._statuses[min(len(self.calls), len(self._statuses)) - 1]
        return FakeResponse(status, {"n": len(self.calls)})


def test_same_session_same_url_fetched_once_even_after_121s():
    s = FakeSession()
    clock = [1000.0]
    with mock.patch.object(http_client.time, "monotonic", side_effect=lambda: clock[0]):
        first = http_client.get_memoized(s, URL, timeout=10)
        clock[0] += 121.0  # 旧 120 秒の期限を越える
        second = http_client.get_memoized(s, URL, timeout=10)
        clock[0] += 3600.0  # 初回・全件取得の長い Snapshot も想定
        third = http_client.get_memoized(s, URL, timeout=10)
    assert s.calls == [URL]
    assert first is second is third


def test_other_session_fetches_again():
    a, b = FakeSession(), FakeSession()
    ra = http_client.get_memoized(a, URL)
    rb = http_client.get_memoized(b, URL)
    assert a.calls == [URL] and b.calls == [URL]
    assert ra is not rb


def test_other_url_fetches_again():
    s = FakeSession()
    http_client.get_memoized(s, URL)
    http_client.get_memoized(s, URL + "/full")
    assert s.calls == [URL, URL + "/full"]


def test_non_200_is_not_memoized():
    for status in (404, 429, 500, 503):
        s = FakeSession(statuses=(status, 200))
        r1 = http_client.get_memoized(s, URL)
        r2 = http_client.get_memoized(s, URL)
        r3 = http_client.get_memoized(s, URL)
        assert r1.status_code == status
        assert r2.status_code == 200
        assert r3 is r2
        assert len(s.calls) == 2  # 失敗の次だけ取り直し、成功後は覚える


def test_forget_memoized_clears_only_that_session():
    a, b = FakeSession(), FakeSession()
    http_client.get_memoized(a, URL)
    http_client.get_memoized(b, URL)
    http_client.forget_memoized(a)
    http_client.forget_memoized(None)  # None でも落ちない
    http_client.get_memoized(a, URL)
    http_client.get_memoized(b, URL)
    assert a.calls == [URL, URL]
    assert b.calls == [URL]


def test_snapshot_end_forgets_memo_of_given_session():
    """呼び出し元のセッションを使い回しても、次の Snapshot では取り直す（成功・失敗のどちらでも）。"""
    s = FakeSession()

    def fake_create(steam_id, session=None, **kwargs):
        http_client.get_memoized(session, URL)
        http_client.get_memoized(session, URL)  # 同じ Snapshot の中では 1 回
        if steam_id == "fail":
            raise RuntimeError("boom")
        return "snap"

    with mock.patch.object(collector, "_create_snapshot_for_steam_id", side_effect=fake_create), \
            mock.patch.object(collector._BackgroundFetch, "cancel_all"):
        assert collector.create_snapshot_for_steam_id("1", session=s) == "snap"
        assert s.calls == [URL]
        assert http_client._GET_MEMO.get(s) is None
        try:
            collector.create_snapshot_for_steam_id("fail", session=s)
        except RuntimeError:
            pass
        assert s.calls == [URL, URL]
        assert http_client._GET_MEMO.get(s) is None


def test_real_callers_share_one_fetch_per_snapshot_session():
    """手順 6・8・スコア取得で同じ URL を読む実際の関数が、時間がたっても 1 回にまとまる。"""
    s = FakeSession()
    clock = [0.0]
    with mock.patch.object(http_client.time, "monotonic", side_effect=lambda: clock[0]):
        bl_basic.fetch_player("1", session=s)
        clock[0] += 300.0
        collector._get_beatleader_player_stats("1", s)
        clock[0] += 300.0
        bl_collector._get_beatleader_player_stats("1", s)
        ss_collector._fetch_scoresaber_player_basic("1", s)
        clock[0] += 300.0
        ss_collector._get_scoresaber_player_stats("1", s)
    bl_urls = [u for u in s.calls if "beatleader" in u]
    ss_urls = [u for u in s.calls if "scoresaber" in u]
    assert bl_urls == ["https://api.beatleader.com/player/1"]
    assert len(ss_urls) == 1 and ss_urls[0].endswith("/player/1/full")
