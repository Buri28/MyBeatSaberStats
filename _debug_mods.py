import json, pathlib, collections, glob

# BL
bl = json.loads(pathlib.Path('cache/beatleader_player_scores_76561198324870685.json').read_text('utf-8'))
scores = bl['scores']
mod_counter = collections.Counter()
na_count = 0

for lb_id, item in scores.items():
    m = str(item.get('modifiers') or '')
    if m:
        for tok in m.split(','):
            tok = tok.strip().upper()
            if tok:
                mod_counter[tok] += 1
    if 'NA' in m.upper():
        pp = item.get('pp')
        print(f'  BL lb_id={lb_id} modifiers={repr(m)} pp={pp}')
        na_count += 1
        if na_count >= 5:
            print('  ...')
            break

print('BL modifiers:', mod_counter.most_common(20))
print()

# SS
for ss_path in glob.glob('cache/scoresaber_player_scores_*.json'):
    ss = json.loads(pathlib.Path(ss_path).read_text('utf-8'))
    scores_ss = ss.get('scores') or ss.get('playerScores') or []
    if isinstance(scores_ss, dict):
        items_ss = list(scores_ss.values())
    else:
        items_ss = scores_ss
    ss_mod_counter = collections.Counter()
    ss_na_count = 0
    for item in items_ss:
        score_obj = item.get('score') if isinstance(item, dict) else item
        if not isinstance(score_obj, dict):
            score_obj = item
        m = str(score_obj.get('modifiers') or '')
        if m:
            for tok in m.split(','):
                tok = tok.strip().upper()
                if tok:
                    ss_mod_counter[tok] += 1
        if 'NA' in m.upper():
            ss_na_count += 1
    print(f'SS [{ss_path}]')
    print(f'  modifiers: {ss_mod_counter.most_common(15)}')
    print(f'  NA count:  {ss_na_count}')
