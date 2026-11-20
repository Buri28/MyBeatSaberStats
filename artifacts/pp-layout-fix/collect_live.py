import sys,json,shutil
from pathlib import Path
from dataclasses import fields
sys.path.insert(0,'src')
from mybeatsaberstats.collector import collector as c
from mybeatsaberstats import snapshot as sn,accsaber_reloaded as ar
from mybeatsaberstats.collector import scoresaber as ss,beatleader as bl
root=Path.cwd();out=root/'artifacts/pp-layout-fix';trial=out/'dist/MyBeatSaberStats'
trial.mkdir(parents=True,exist_ok=True)
shutil.copytree(root/'cache',trial/'cache',dirs_exist_ok=True)
shutil.copytree(root/'snapshots',trial/'snapshots',dirs_exist_ok=True)
# Redirect all path constants into the isolated executable data directory.
for module in (c,sn,ar,ss,bl):
 for name,value in list(vars(module).items()):
  if isinstance(value,Path) and value.is_relative_to(root):
   setattr(module,name,trial/value.relative_to(root))
options=c.SnapshotOptions(**{f.name:False for f in fields(c.SnapshotOptions) if f.name.startswith('fetch_')})
options.fetch_accsaber_reloaded=True
options.fetch_beatleader=True
options.fetch_ss_star_stats=True
options.fetch_bl_star_stats=True
warnings=[]
r=c.create_snapshot_for_steam_id('76561198324870685',options=options,snapshot_dir=trial/'snapshots',on_warning=warnings.append)
r.save(out/'live-snapshot.json')
print('RESULT',r.accsaber_fetch_status,r.accsaber_reloaded_overall_ap,r.beatleader_pass_pp,warnings)
(out/'live-snapshot-result.json').write_text(json.dumps({'status':r.accsaber_fetch_status,'ap':r.accsaber_reloaded_overall_ap,'warnings':warnings,'taken_at':r.taken_at},indent=2),encoding='utf-8')
# Enable the requested service in the trial without changing the user's running app settings.
p=trial/'cache/take_snapshot_dialog.json';d=json.loads(p.read_text(encoding='utf-8'));d['fetch_accsaber_rl']=True;d['fetch_beatleader']=True;p.write_text(json.dumps(d,indent=2),encoding='utf-8')
