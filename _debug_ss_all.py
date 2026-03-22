import json, pathlib, glob

d = json.loads(pathlib.Path('cache/scoresaber_ranked_maps.json').read_text('utf-8'))
lbs = d['leaderboards']
ranked_lb_ids = set(str(v['id']) for v in lbs.values() if isinstance(v, dict) and v.get('id'))
print(f'Ranked cache lb_ids: {len(ranked_lb_ids)}')

for ss_path in sorted(glob.glob('cache/scoresaber_player_scores_*.json')):
    ss = json.loads(pathlib.Path(ss_path).read_text('utf-8'))
    scores = ss['scores']
    in_clear = in_nf = in_na = in_ss = 0
    miss_pos = 0
    for lb_id, item in scores.items():
        lb = item.get('leaderboard', {})
        score = item.get('score', {})
        stars = lb.get('stars') or 0
        mod = str(score.get('modifiers') or '').upper()
        is_na = 'NA' in mod; is_nf = 'NF' in mod; is_ss = 'SS' in mod
        if lb_id in ranked_lb_ids:
            if is_nf: in_nf += 1
            elif is_na: in_na += 1
            elif is_ss: in_ss += 1
            else: in_clear += 1
        elif stars > 0:
            miss_pos += 1
    total_in = in_clear + in_nf + in_na + in_ss
    pid = ss_path.split('scoresaber_player_scores_')[1].replace('.json','')
    print(f'{pid}: in-cache clear={in_clear} NF={in_nf} NA={in_na} SS={in_ss} sum={total_in}  cache-miss(stars>0)={miss_pos}  would_be={total_in+miss_pos}')
