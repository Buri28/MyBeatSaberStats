"""
"astrid" Hard (ssId=364318, blId=1985451) がプレイ済みと判定される理由を調査
"""
import sys, json
sys.path.insert(0, "src")
from pathlib import Path

BASE_DIR = Path(".")
TARGET_HASH = "47d574d2274c36b45a63a7b808bcf74b49a0a3ba"
TARGET_DIFF = "Hard"
TARGET_CHAR = "Standard"
TARGET_BL_ID = "1985451"
TARGET_SS_ID = "364318"

_rl_invalid_mods = frozenset({"NO", "NB", "SS", "SC", "SN"})
_ss_diff_map = {1: "Easy", 3: "Normal", 5: "Hard", 7: "Expert", 9: "ExpertPlus"}
_ss_mode_to_char = {
    "SoloStandard": "Standard", "SoloOneSaber": "OneSaber",
    "SoloNoArrows": "NoArrows", "SoloLightShow": "Lightshow",
    "Solo360Degree": "360Degree", "Solo90Degree": "90Degree",
}

steam_id = "76561198324870685"
bl_id = steam_id
ss_id = steam_id

print("=== BL cache check ===")
bl_path = BASE_DIR / "cache" / f"beatleader_player_scores_{bl_id}.json"
bl_matched = []
bl_target_entries = []
if bl_path.exists():
    bl_data = json.loads(bl_path.read_text(encoding="utf-8"))
    for score_val in (bl_data.get("scores") or {}).values():
        lb = score_val.get("leaderboard") or {}
        lb_id_val = str(lb.get("id") or "")
        song = lb.get("song") or {}
        h = song.get("hash", "").lower()
        mods = str((score_val.get("score") or {}).get("modifiers") or "")
        diff_obj = lb.get("difficulty") or {}
        mode = diff_obj.get("modeName", "Standard")
        diff_name = diff_obj.get("difficultyName", "")
        
        if lb_id_val == TARGET_BL_ID or h == TARGET_HASH:
            invalid = bool(mods and frozenset(t.strip().upper() for t in mods.split(",")) & _rl_invalid_mods)
            bl_target_entries.append({
                "lb_id": lb_id_val, "hash": h, "mode": mode, "diff": diff_name,
                "mods": mods, "filtered": invalid
            })
        
        if lb_id_val == TARGET_BL_ID and not (mods and frozenset(t.strip().upper() for t in mods.split(",")) & _rl_invalid_mods):
            bl_matched.append(("bl_id", lb_id_val, mods))

print(f"BL entries for astrid (hash or blId match):")
for e in bl_target_entries:
    print(f"  lb_id={e['lb_id']} diff={e['diff']} mods='{e['mods']}' filtered={e['filtered']}")
print(f"BL ID match (after filter): {bl_matched}")

print()
print("=== SS cache check ===")
ss_path = BASE_DIR / "cache" / f"scoresaber_player_scores_{ss_id}.json"
ss_matched_id = []
ss_hash_matched = []
if ss_path.exists():
    ss_data = json.loads(ss_path.read_text(encoding="utf-8"))
    for score_val in (ss_data.get("scores") or {}).values():
        mods = str((score_val.get("score") or {}).get("modifiers") or "")
        invalid = bool(mods and frozenset(t.strip().upper() for t in mods.split(",")) & _rl_invalid_mods)
        lb = score_val.get("leaderboard") or {}
        ss_lb_id = str(lb.get("id") or "")
        song_hash = lb.get("songHash", "").lower()
        diff_obj = lb.get("difficulty") or {}
        diff_num = diff_obj.get("difficulty")
        game_mode = diff_obj.get("gameMode", "SoloStandard")
        diff_name = _ss_diff_map.get(int(diff_num)) if diff_num is not None else None
        char = _ss_mode_to_char.get(game_mode, game_mode.replace("Solo", ""))
        
        if ss_lb_id == TARGET_SS_ID and not invalid:
            ss_matched_id.append(("ss_id", ss_lb_id, mods))
        if song_hash == TARGET_HASH and diff_name == TARGET_DIFF and char == TARGET_CHAR and not invalid:
            ss_hash_matched.append(("hash", ss_lb_id, mods, diff_name))

print(f"SS ID match (after filter) for {TARGET_SS_ID}: {ss_matched_id}")
print(f"SS hash match (after filter) for Hard: {ss_hash_matched}")

print()
print("=== played_set check for (hash, Standard, Hard) ===")
played_set = set()
# rebuild played_set from both caches
if bl_path.exists():
    bl_data = json.loads(bl_path.read_text(encoding="utf-8"))
    for score_val in (bl_data.get("scores") or {}).values():
        mods = str((score_val.get("score") or {}).get("modifiers") or "")
        if mods and frozenset(t.strip().upper() for t in mods.split(",")) & _rl_invalid_mods:
            continue
        lb = score_val.get("leaderboard") or {}
        song = lb.get("song") or {}
        h = song.get("hash", "").lower()
        diff_obj = lb.get("difficulty") or {}
        mode = diff_obj.get("modeName", "Standard")
        diff_name = diff_obj.get("difficultyName", "")
        if h and diff_name:
            played_set.add((h, mode, diff_name))

if ss_path.exists():
    ss_data = json.loads(ss_path.read_text(encoding="utf-8"))
    for score_val in (ss_data.get("scores") or {}).values():
        mods = str((score_val.get("score") or {}).get("modifiers") or "")
        if mods and frozenset(t.strip().upper() for t in mods.split(",")) & _rl_invalid_mods:
            continue
        lb = score_val.get("leaderboard") or {}
        song_hash = lb.get("songHash", "").lower()
        diff_obj = lb.get("difficulty") or {}
        diff_num = diff_obj.get("difficulty")
        game_mode = diff_obj.get("gameMode", "SoloStandard")
        diff_name = _ss_diff_map.get(int(diff_num)) if diff_num is not None else None
        char = _ss_mode_to_char.get(game_mode, game_mode.replace("Solo", ""))
        if song_hash and diff_name:
            played_set.add((song_hash, char, diff_name))

key = (TARGET_HASH, TARGET_CHAR, TARGET_DIFF)
print(f"  {key} in played_set: {key in played_set}")

# show all entries for this hash
hash_entries = [(h, c, d) for (h, c, d) in played_set if h == TARGET_HASH]
print(f"  All played_set entries for this hash: {hash_entries}")
