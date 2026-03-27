"""
RL APIでBuriTatsutaのスコア一覧を取得し、
BLキャッシュにあるがRLにないスコアを特定する
"""
import sys, json, urllib.request
sys.path.insert(0, "src")
from pathlib import Path

USER_ID = "76561198324870685"
CATEGORY_ID = "b0000000-0000-0000-0000-000000000002"
BASE_URL = "https://api.accsaberreloaded.com"

def fetch(path):
    req = urllib.request.Request(BASE_URL + path, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)

# RL側のユーザーIDを探す
print("Fetching RL user info...")
try:
    user = fetch(f"/v1/users/{USER_ID}")
    rl_user_id = user.get("id") or user.get("userId") or USER_ID
    print(f"RL User: {user.get('name')} id={rl_user_id}")
except Exception as e:
    print(f"Error: {e}")
    rl_user_id = USER_ID

# RL側のスコア一覧取得 (Standard only)
print(f"\nFetching RL scores for user {rl_user_id} (Standard)...")
rl_diff_ids = set()
page = 0
while True:
    try:
        data = fetch(f"/v1/users/{rl_user_id}/scores?categoryId={CATEGORY_ID}&page={page}&size=100")
        content = data.get("content", [])
        if not content:
            break
        for score in content:
            diff_id = score.get("mapDifficultyId") or ""
            rl_diff_ids.add(diff_id)
        print(f"  page {page}: {len(content)} scores, total so far: {len(rl_diff_ids)}")
        if data.get("last", False) or page >= data.get("totalPages", 1) - 1:
            break
        page += 1
    except Exception as e:
        print(f"  Error on page {page}: {e}")
        break

print(f"\nTotal RL scored difficulties (Standard): {len(rl_diff_ids)}")
