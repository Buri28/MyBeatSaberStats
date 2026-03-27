"""
修正後の動作確認: RL APIを使ったプレイ済み判定で20件の未プレイが正しく出るか確認
"""
import sys, json, urllib.request
sys.path.insert(0, "src")

from mybeatsaberstats.accsaber_reloaded import (
    fetch_player_scored_diff_ids,
    fetch_all_maps_full,
    build_unplayed_bplist,
)
import requests

USER_ID = "76561198324870685"  # BuriTatsuta (BL ID)

print("1. Fetching RL player scored diff IDs...")
session = requests.Session()
scored_ids = fetch_player_scored_diff_ids(USER_ID, session)
for cat, ids in scored_ids.items():
    print(f"  {cat}: {len(ids)} scored diffs")

print("\n2. Fetching all RL maps...")
all_maps = fetch_all_maps_full(session=session)
print(f"  Total songs: {len(all_maps)}")

print("\n3. Building unplayed bplist for standard (using RL scored IDs)...")
bplist = build_unplayed_bplist(
    all_maps,
    played_set=set(),
    category="standard",
    played_rl_diff_ids=scored_ids.get("standard"),
)
songs = bplist.get("songs", [])
n_diffs = sum(len(s.get("difficulties", [])) for s in songs)
print(f"  Unplayed: {n_diffs} diffs in {len(songs)} songs")

print("\n=== Standard Unplayed Songs ===")
for song in sorted(songs, key=lambda s: s.get("songName", "")):
    diffs = [d.get("name") for d in song.get("difficulties", [])]
    print(f"  {song.get('songName')} / {', '.join(diffs)}")

# astrid が含まれているか確認
astrid_found = any("astrid" in s.get("songName", "").lower() for s in songs)
print(f"\nastrid in unplayed list: {astrid_found} (expected: True)")
print(f"Total unplayed diffs: {n_diffs} (expected: 20)")
