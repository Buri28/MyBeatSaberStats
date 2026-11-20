import json, shutil, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
sys.path.insert(0, "src")
from mybeatsaberstats.collector import scoresaber as ss
from mybeatsaberstats.http_client import make_session
tmp = Path(tempfile.mkdtemp())
orig = json.loads(Path("cache/scoresaber_ranked_maps.json").read_text(encoding="utf-8"))
lbs = dict(orig["leaderboards"])
old = sorted([k for k, v in lbs.items() if (v.get("rankedDate") or "9") < "2025-01-01"])[:3]
for k in old: del lbs[k]
orig2 = dict(orig); orig2["leaderboards"] = lbs
(tmp / "scoresaber_ranked_maps.json").write_text(json.dumps(orig2), encoding="utf-8")
until = datetime.utcnow() - timedelta(days=60)
with mock.patch.object(ss, "CACHE_DIR", tmp), mock.patch.object(ss, "_RANKED_FULL_TRAVERSAL_STATE", tmp / "t.json"):
    res = ss._get_scoresaber_leaderboards_ranked(make_session(), fetch_until=until)
ids = {str(k) for k in res}
print("REMOVED_RESTORED", all(k in ids for k in old), len(ids), "orig", len(orig["leaderboards"]))
diff = [k for k, v in res.items() if str(k) in orig["leaderboards"] and (v.get("stars") != orig["leaderboards"][str(k)].get("stars"))]
print("STARS_CHANGED_COUNT", len(diff), diff[:10])
