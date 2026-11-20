"""rankedDate が null の譜面が先頭に並んでも、新しいランク譜面を取り込む回帰テスト。"""
import sys
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats.collector import scoresaber as ss  # noqa: E402


class _Resp:
    status_code = 200
    url = "x"
    headers: dict = {}

    def __init__(self, data):
        self._d = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def _lb(i, ranked):
    return {"id": i, "rankedDate": ranked, "createdDate": "2018-07-01T00:00:00.000Z"}


def test_new_ranked_after_null_dates(tmp_path):
    # API は rankedDate 降順: null（古い譜面）→ 新しい → 古い（境界より前）
    pages = {
        1: [_lb(1, None), _lb(2, None)],
        2: [_lb(3, "2026-11-15T00:00:00.000Z"), _lb(4, "2026-10-11T00:00:00.000Z")],
        3: [_lb(5, "2020-01-01T00:00:00.000Z"), _lb(6, "2020-01-01T00:00:00.000Z")],
    }

    class S:
        def get(self, url, params=None, timeout=None):
            return _Resp({"metadata": {"total": 6, "itemsPerPage": 2},
                          "leaderboards": pages.get(int(params["page"]), [])})

    import json
    from datetime import timedelta
    (tmp_path / "scoresaber_ranked_maps.json").write_text(json.dumps({
        "fetched_at": "2026-11-17T00:00:00.000000Z", "max_pages": 1, "total_maps": 2,
        "leaderboards": {"5": _lb(5, "2020-01-01T00:00:00.000Z"), "6": _lb(6, "2020-01-01T00:00:00.000Z")}}), encoding="utf-8")
    _st = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "traversal.json").write_text(json.dumps({"scoresaber": _st, "scoresaber_complete": _st}), encoding="utf-8")
    with mock.patch.object(ss, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"), \
         mock.patch.object(ss, "CACHE_DIR", tmp_path), \
         mock.patch.object(ss, "_is_rate_limited", return_value=False):
        result = ss._get_scoresaber_leaderboards_ranked(S(), fetch_until=datetime(2026, 9, 1))
    ids = {str(k) for k in result}
    assert {"1", "2", "3", "4"} <= ids, ids
    assert ids == {"1", "2", "3", "4", "5", "6"}  # 境界より古い 5・6 はキャッシュにあり、総数も合う（全件取り直しにならない）


if __name__ == "__main__":
    import tempfile
    test_new_ranked_after_null_dates(Path(tempfile.mkdtemp()))
    print("OK")
