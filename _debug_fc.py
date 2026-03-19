import json, pathlib, sys
sys.path.insert(0, "src")
from mybeatsaberstats.snapshot import Snapshot

snapshots_dir = pathlib.Path("snapshots")
files = sorted(snapshots_dir.glob("76561198324870685_*.json"))
snap_old = Snapshot.load(files[0])
snap_new = Snapshot.load(files[-1])
print("old file:", files[0].name)
print("new file:", files[-1].name)
print()
for s_old, s_new in zip(snap_old.beatleader_star_stats[:6], snap_new.beatleader_star_stats[:6]):
    print("BL star=%d  old_fc=%s  new_fc=%s" % (s_old.star, s_old.fc_count, s_new.fc_count))
print()
for s_old, s_new in zip(snap_old.star_stats[:6], snap_new.star_stats[:6]):
    print("SS star=%d  old_fc=%s  new_fc=%s" % (s_old.star, s_old.fc_count, s_new.fc_count))
