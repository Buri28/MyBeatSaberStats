"""保存済みの SS ランク譜面キャッシュのコピーに、保存した 11 月のページを当てて判定を数える（本物の cache は書き換えない）。

本当の全件取得は 11/18 なので、そのままだと 11 月のページは「全件取得より前」で読み飛ばす。判定を数えるため、
コピーの scoresaber_complete を 11/01 にして 11 月のページの各行を判定させる。wiki・SS API には接続しない。
"""
import json, shutil, sys, tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from mybeatsaberstats import ss_reweights as sr  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "ss_reweights"
NOV_URL = "https://wiki.scoresaber.com/ranking/reweights/2026/November-2026.html"


class Resp:
    def __init__(self, text):
        self.status_code, self.text = 200, text


class Session:
    calls = []
    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(url)
        pages = {sr.REWEIGHTS_INDEX_URL: (FIX / "reweights_index.html").read_text(encoding="utf-8"),
                 NOV_URL: (FIX / "November-2026.html").read_text(encoding="utf-8")}
        if url in pages:
            return Resp(pages[url])
        raise AssertionError(f"unexpected call {url}")


with tempfile.TemporaryDirectory() as tmp:
    cache = Path(tmp) / "cache"
    cache.mkdir()
    shutil.copy(ROOT / "cache" / "scoresaber_ranked_maps.json", cache)
    real_traversal = json.loads((ROOT / "cache" / "ranked_full_traversal.json").read_text(encoding="utf-8"))
    print("real scoresaber_complete:", real_traversal.get("scoresaber_complete"))
    for label, complete in [("as-is", real_traversal["scoresaber_complete"]), ("complete=2026-11-01", "2026-11-01T00:00:00Z")]:
        # 前月（10 月）のページも読む対象になるが、wiki のアクセスは 2 回までなので取得していない。処理済みにして読まない
        (cache / sr.STATE_FILE_NAME).write_text(json.dumps({"processed": {"2026/October-2026": {"at": "verification"}}}), encoding="utf-8")
        t = dict(real_traversal, scoresaber_complete=complete)
        (cache / "ranked_full_traversal.json").write_text(json.dumps(t), encoding="utf-8")
        s = Session(); s.calls = []
        summary = sr.apply_scoresaber_reweights(s, cache, now=datetime(2026, 11, 21, 12, tzinfo=timezone.utc))
        print(f"--- {label}: status={summary['status']} calls={len(s.calls)}")
        for page in summary["pages"]:
            print({k: v for k, v in page.items() if k != "rows"})
            for r in page["rows"]:
                print(f"  {r['verdict']:9} {r['leaderboard_id']}  {r['name']} | {r['difficulty']} | {r['mapper']} | {r['old']} -> {r['new']}")
