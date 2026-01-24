import sys
import requests

from mybeatsaberstats.collector.collector import _get_scoresaber_player_scores

print('starting script')
sys.path.insert(0, 'src')
print('imported ok')
s = requests.Session()
res = _get_scoresaber_player_scores('3117609721598571', s)
print('len', len(res))
if res:
    print(list(res[0].keys()))
