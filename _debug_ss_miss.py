import json, pathlib

# SSランクキャッシュのlb_idセット
ss_ranked = json.loads(pathlib.Path('cache/scoresaber_ranked_maps.json').read_text('utf-8'))
leaderboards = ss_ranked.get('leaderboards', [])
ranked_lb_ids = set()
for item in leaderboards:
    if isinstance(item, dict):
        lb_id = item.get('id') or item.get('leaderboardId')
        if lb_id:
            ranked_lb_ids.add(str(lb_id))
print(f'Ranked cache lb_ids: {len(ranked_lb_ids)}')

# player 42884990 のSSスコアから、キャッシュ外でstars>0のものを数える
ss_scores = json.loads(pathlib.Path('cache/scoresaber_player_scores_76561198042884990.json').read_text('utf-8'))
scores = ss_scores['scores']
print(f'Total player scores: {len(scores)}')

miss_pos = 0
miss_zero = 0
miss_na_pos = 0
miss_na_zero = 0
in_cache_na = 0
in_cache_clear = 0
in_cache_nf = 0

for lb_id, item in scores.items():
    lb = item.get('leaderboard', {})
    score = item.get('score', {})
    stars = lb.get('stars') or 0
    mod = str(score.get('modifiers') or '').upper()
    is_na = 'NA' in mod
    is_nf = 'NF' in mod

    if lb_id in ranked_lb_ids:
        if is_na:
            in_cache_na += 1
        elif is_nf:
            in_cache_nf += 1
        else:
            in_cache_clear += 1
    else:
        if stars > 0:
            miss_pos += 1
            if is_na:
                miss_na_pos += 1
        else:
            miss_zero += 1
            if is_na:
                miss_na_zero += 1

print(f'In ranked cache: clear={in_cache_clear}, NF={in_cache_nf}, NA={in_cache_na}')
print(f'Cache-miss, stars>0: {miss_pos} (NA={miss_na_pos})')
print(f'Cache-miss, stars=0: {miss_zero} (NA={miss_na_zero})')
print(f'Expected total (in-cache): {in_cache_clear + in_cache_nf + in_cache_na}')
print(f'Expected total (in-cache + stars>0): {in_cache_clear + in_cache_nf + in_cache_na + miss_pos}')
