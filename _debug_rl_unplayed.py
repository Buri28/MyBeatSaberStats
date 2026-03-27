"""
RL APIで直接 BuriTatsuta の未プレイマップを特定し、
ツールの出力と比較する
"""
import sys, json, urllib.request
sys.path.insert(0, "src")

BASE = "https://api.accsaberreloaded.com"
USER_ID = "76561198324870685"
CATEGORY_ID = "b0000000-0000-0000-0000-000000000002"

def fetch(path):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)

# --- 1. RL標準マップ全件取得 (/v1/maps から difficulty.id を収集) ---
print("Fetching RL Standard maps...")
all_diff_ids = {}   # diff_uuid -> diff_info
page = 0
while True:
    data = fetch(f"/v1/maps?page={page}&size=100")
    content = data.get("content", [])
    if not content:
        break
    for song in content:
        song_hash = song.get("songHash", "").lower()
        song_name = song.get("songName", "")
        for diff in song.get("difficulties", []):
            if not diff.get("active", False):
                continue
            if diff.get("categoryId") != CATEGORY_ID:
                continue
            diff_id = diff.get("id")
            if diff_id:
                all_diff_ids[diff_id] = {
                    "songName": song_name,
                    "songHash": song_hash,
                    "difficulty": diff.get("difficulty"),
                    "blLeaderboardId": diff.get("blLeaderboardId"),
                    "ssLeaderboardId": diff.get("ssLeaderboardId"),
                }
    if data.get("last", False):
        break
    page += 1

print(f"Total RL Standard diffs: {len(all_diff_ids)}")

# --- 2. BuriTatsutaのRLスコア全件取得 ---
print("\nFetching BuriTatsuta RL scores...")
rl_scored_ids = set()
page = 0
while True:
    data = fetch(f"/v1/users/{USER_ID}/scores?categoryId={CATEGORY_ID}&page={page}&size=100")
    content = data.get("content", [])
    if not content:
        break
    for score in content:
        diff_id = score.get("mapDifficultyId")
        if diff_id:
            rl_scored_ids.add(diff_id)
    if data.get("last", False) or page >= data.get("totalPages", 1) - 1:
        break
    page += 1
print(f"Total RL scored: {len(rl_scored_ids)}")

# --- 3. RL未プレイマップ特定 ---
unplayed_ids = set(all_diff_ids.keys()) - rl_scored_ids
print(f"\nRL unplayed diffs: {len(unplayed_ids)}")

print("\n=== RL Unplayed Maps (Ground Truth) ===")
unplayed_sorted = sorted(unplayed_ids, key=lambda x: all_diff_ids[x].get("songName",""))
for diff_id in unplayed_sorted:
    d = all_diff_ids[diff_id]
    song_hash = d.get("songHash","").lower()
    bl_id = d.get("blLeaderboardId","")
    ss_id = d.get("ssLeaderboardId","")
    print(f"  {d.get('songName','')} / {d.get('difficulty','')} "
          f"(hash={song_hash[:8]}.. bl={bl_id} ss={ss_id})")
