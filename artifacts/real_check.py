"""実 API で、キャッシュのコピーを使って全件取り直しを確かめる（実キャッシュには触らない）。"""
import json, shutil, sys, tempfile, time
from datetime import datetime, timedelta
from pathlib import Path
sys.path.insert(0, r"C:\work\tool\MyBeatSaberStats\src")
from unittest import mock
from mybeatsaberstats.http_client import make_session
from mybeatsaberstats.collector import scoresaber as ss, beatleader as bl

real = Path(r"C:\work\tool\MyBeatSaberStats\cache")
tmp = Path(tempfile.mkdtemp(prefix="rankedcopy_"))
for n in ("scoresaber_ranked_maps.json", "beatleader_ranked_maps.json"):
    shutil.copy(real / n, tmp / n)
which = sys.argv[1]
session = make_session()
t0 = time.time()
calls = {"n": 0}
orig = session.get
def counted(*a, **k):
    calls["n"] += 1
    return orig(*a, **k)
session.get = counted
state = tmp / "traversal.json"
if which == "ss":
    old = json.loads((tmp / "scoresaber_ranked_maps.json").read_text(encoding="utf-8"))
    fa = datetime.strptime(old["fetched_at"][:19], "%Y-%m-%dT%H:%M:%S")
    with mock.patch.object(ss, "CACHE_DIR", tmp), mock.patch.object(ss, "_RANKED_FULL_TRAVERSAL_STATE", state):
        res = ss._get_scoresaber_leaderboards_ranked(session, fetch_until=fa - timedelta(days=60))
        print("RUN1 calls", calls["n"], "secs", int(time.time() - t0), "maps", len(res), file=sys.stderr)
        c1 = calls["n"]
        res2 = ss._get_scoresaber_leaderboards_ranked(session, fetch_until=datetime.utcnow() - timedelta(days=61))
        print("RUN2(窓のみ) calls", calls["n"] - c1, "maps", len(res2), file=sys.stderr)
    new = json.loads((tmp / "scoresaber_ranked_maps.json").read_text(encoding="utf-8"))["leaderboards"]
    oldl = old["leaderboards"]
    print("old", len(oldl), "new", len(new))
    print("added", sorted(set(new) - set(oldl))[:20], "removed", sorted(set(oldl) - set(new))[:20])
    chg = [k for k in new if k in oldl and (new[k].get("stars"), new[k].get("rankedDate")) != (oldl[k].get("stars"), oldl[k].get("rankedDate"))]
    print("★/rankedDate changed", [(k, oldl[k].get("stars"), new[k].get("stars")) for k in chg][:20], len(chg))
    for k in ("1819224", "702820"):
        print(k, "old stars", oldl.get(k, {}).get("stars"), "new stars", new.get(k, {}).get("stars"))
    print("state", state.read_text(encoding="utf-8").replace("\n", " "))
    (tmp / "result_ss.json").write_text(json.dumps({"ids": sorted(new), "stars": {k: v.get("stars") for k, v in new.items()}}), encoding="utf-8")
    r = session.get("https://scoresaber.com/api/leaderboards", params={"ranked": "true", "page": "1"}, timeout=15)
    print("API total", r.json()["metadata"]["total"])
    for k in ("1819224", "702820"):
        r = session.get(f"https://scoresaber.com/api/leaderboard/by-id/{k}/info", timeout=15)
        print("API", k, "stars", r.json().get("stars"), "ranked", r.json().get("ranked"))
else:
    old = bl._load_cached_ranked_leaderboards(tmp / "beatleader_ranked_maps.json")
    oldd = {str(x["id"]): x for x in old}
    with mock.patch.object(bl, "CACHE_DIR", tmp), mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", state):
        raw = json.loads((tmp / "beatleader_ranked_maps.json").read_text(encoding="utf-8"))
        fa = datetime.strptime(raw["fetched_at"][:19], "%Y-%m-%dT%H:%M:%S")
        res = bl._get_beatleader_leaderboards_ranked(session, fetch_until=fa - timedelta(days=60))
        print("RUN1 calls", calls["n"], "secs", int(time.time() - t0), "maps", len(res), file=sys.stderr)
        c1 = calls["n"]
        res2 = bl._get_beatleader_leaderboards_ranked(session, fetch_until=datetime.utcnow() - timedelta(days=61))
        print("RUN2(窓のみ) calls", calls["n"] - c1, "maps", len(res2), file=sys.stderr)
    newd = {str(x["id"]): x for x in bl._load_cached_ranked_leaderboards(tmp / "beatleader_ranked_maps.json")}
    print("old", len(oldd), "new", len(newd))
    print("added", sorted(set(newd) - set(oldd))[:20], "removed", sorted(set(oldd) - set(newd))[:20])
    chg = [k for k in newd if k in oldd and newd[k]["difficulty"].get("stars") != oldd[k]["difficulty"].get("stars")]
    print("★ changed", len(chg), [(k, oldd[k]["difficulty"].get("stars"), newd[k]["difficulty"].get("stars")) for k in chg][:10])
    print("state", state.read_text(encoding="utf-8").replace("\n", " "))
    r = session.get("https://api.beatleader.xyz/leaderboards", params={"page": "1", "count": "1", "type": "Ranked"}, timeout=20)
    print("API total", r.json()["metadata"]["total"])
print("tmp", tmp)
