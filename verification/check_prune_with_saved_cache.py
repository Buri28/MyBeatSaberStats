"""保存済みキャッシュのコピーで、PP の取り直しで返らなかったスコアを外す処理を確かめる（API は呼ばない）。

cache/ の実ファイルは読むだけ。一時フォルダへコピーし、CACHE_DIR をそこへ向けて実行する。
キャッシュ上のランク譜面 pp>0 のスコアから模擬応答を作り、
  (1) 全部返す → 何も外れず、★統計（Clears など）が全く変わらない
  (2) 1 件だけ抜く → 外れるのはその 1 件だけ。★統計の変化はその 1 件の分だけ
を確かめる。実行: .venv\\Scripts\\python.exe verification\\check_prune_with_saved_cache.py
"""
import json
import shutil
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mybeatsaberstats.collector import beatleader as bl  # noqa: E402
from mybeatsaberstats.collector import scoresaber as ss  # noqa: E402

SID = "76561198324870685"
FIELDS = ("map_count", "clear_count", "nf_count", "ss_count", "na_count", "pp_contribution")


class _Resp:
    status_code = 200
    url = "x"
    headers: dict = {}

    def __init__(self, d):
        self._d = d

    def raise_for_status(self):
        pass

    def json(self):
        return self._d


def stats_table(stats):
    return {s.star: {k: (round(v, 6) if isinstance(v, float) else v) for k, v in asdict(s).items() if k in FIELDS}
            for s in stats}


def diff_tables(a, b):
    out = {}
    for star in sorted(set(a) | set(b)):
        ra, rb = a.get(star, {}), b.get(star, {})
        d = {k: (ra.get(k), rb.get(k)) for k in FIELDS if ra.get(k) != rb.get(k)}
        if d:
            out[star] = d
    return out


def copy_cache(tmp, names):
    for n in names:
        shutil.copy2(ROOT / "cache" / n, tmp / n)


# ---------------- BeatLeader ----------------

def bl_ranked_items(scores, ranked_ids):
    def pp(it):
        try:
            return float(it.get("pp") or 0)
        except (TypeError, ValueError):
            return 0.0
    items = [it for k, it in scores.items() if isinstance(it, dict)
             and ((it.get("leaderboard") or {}).get("difficulty") or {}).get("status") == 3 and pp(it) > 0]
    items.sort(key=lambda it: -pp(it))
    return items


class BlApi:
    def __init__(self, items):
        self.items = items
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        page = int(params["page"])
        part = self.items[(page - 1) * 100: page * 100]
        return _Resp({"metadata": {"total": len(self.items)}, "data": part})


def check_bl():
    names = ["beatleader_ranked_maps.json", f"beatleader_player_scores_{SID}.json"]
    results = {}
    for mode in ("all", "omit1"):
        tmp = Path(tempfile.mkdtemp(prefix="prune_bl_"))
        copy_cache(tmp, names)
        with mock.patch.object(bl, "CACHE_DIR", tmp):
            path = tmp / names[1]
            before_scores = bl._load_cached_player_scores(path)
            failed_before = bl._load_cached_player_score_failed_pages(path)
            ranked_ids = {str(lb.get("id")) for lb in bl._load_cached_ranked_leaderboards(tmp / names[0])
                          if (lb.get("difficulty") or {}).get("status") == 3}
            items = bl_ranked_items(before_scores, ranked_ids)
            omitted = None
            if mode == "omit1":
                # ランク譜面キャッシュにある譜面のスコアを 1 件抜く（PP 合計の変化が見えるよう、pp の高い方から 11 番目）
                cands = [it for it in items if str(it["leaderboard"]["id"]) in ranked_ids]
                omitted = cands[10]
                items = [it for it in items if it is not omitted]
            stats_before = stats_table(bl.collect_beatleader_star_stats_from_cache(SID))
            st = {"complete": True}
            n = bl.refresh_beatleader_ranked_scores(SID, BlApi(items), status=st)
            after_scores = bl._load_cached_player_scores(path)
            stats_after = stats_table(bl.collect_beatleader_star_stats_from_cache(SID))
            removed = sorted(set(before_scores) - set(after_scores))
            results[mode] = {
                "cached_scores": len(before_scores), "returned": n, "complete": st["complete"],
                "removed": removed,
                "omitted": str(omitted["leaderboard"]["id"]) if omitted else None,
                "omitted_pp": omitted.get("pp") if omitted else None,
                "omitted_stars": ((omitted or {}).get("leaderboard") or {}).get("difficulty", {}).get("stars"),
                "omitted_modifiers": omitted.get("modifiers") if omitted else None,
                "failed_pages_kept": bl._load_cached_player_score_failed_pages(path) == failed_before,
                "failed_pages": failed_before,
                "stats_diff": diff_tables(stats_before, stats_after),
                "clears_before": sum(v["clear_count"] for v in stats_before.values()),
                "clears_after": sum(v["clear_count"] for v in stats_after.values()),
                "pp_total_before": round(sum((v["pp_contribution"] or 0) for v in stats_before.values()), 3),
                "pp_total_after": round(sum((v["pp_contribution"] or 0) for v in stats_after.values()), 3),
            }
        shutil.rmtree(tmp, ignore_errors=True)
    return results


# ---------------- ScoreSaber ----------------

def ss_pp(it):
    try:
        return float((it.get("score") or {}).get("pp") or 0)
    except (TypeError, ValueError):
        return 0.0


class SsApi:
    def __init__(self, items, tail):
        self.items = items + tail
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        page = int(params["page"])
        return _Resp({"playerScores": self.items[(page - 1) * 100: page * 100]})


def check_ss():
    names = ["scoresaber_ranked_maps.json", f"scoresaber_player_scores_{SID}.json"]
    results = {}
    for mode in ("all", "omit1"):
        tmp = Path(tempfile.mkdtemp(prefix="prune_ss_"))
        copy_cache(tmp, names)
        with mock.patch.object(ss, "CACHE_DIR", tmp):
            path = tmp / names[1]
            raw_before = json.loads(path.read_text(encoding="utf-8"))
            before_scores = raw_before["scores"]
            ranked_ids = set(ss._normalize_leaderboard_cache(ss._load_cached_rank_maps(tmp / names[0]) or {}))
            items = [it for it in before_scores.values() if isinstance(it, dict)
                     and (it.get("leaderboard") or {}).get("ranked") and ss_pp(it) > 0]
            items.sort(key=lambda it: -ss_pp(it))
            tail = [it for it in before_scores.values() if isinstance(it, dict) and ss_pp(it) <= 0][:5]
            omitted = None
            if mode == "omit1":
                cands = [it for it in items if str(it["leaderboard"]["id"]) in ranked_ids]
                omitted = cands[10]
                items = [it for it in items if it is not omitted]
            stats_before = stats_table(ss.collect_scoresaber_star_stats_from_cache(SID))
            st = {"complete": True}
            n = ss.refresh_scoresaber_ranked_scores(SID, SsApi(items, tail), status=st)
            raw_after = json.loads(path.read_text(encoding="utf-8"))
            after_scores = raw_after["scores"]
            stats_after = stats_table(ss.collect_scoresaber_star_stats_from_cache(SID))
            removed = sorted(set(before_scores) - set(after_scores))
            results[mode] = {
                "cached_scores": len(before_scores), "returned": n, "complete": st["complete"],
                "removed": removed,
                "omitted": str(omitted["leaderboard"]["id"]) if omitted else None,
                "omitted_pp": ss_pp(omitted) if omitted else None,
                "omitted_stars": (omitted or {}).get("leaderboard", {}).get("stars"),
                "omitted_modifiers": (omitted or {}).get("score", {}).get("modifiers"),
                "resume_stop_ts_kept": raw_after.get("resume_stop_ts") == raw_before.get("resume_stop_ts"),
                "resume_stop_ts": raw_before.get("resume_stop_ts"),
                "stats_diff": diff_tables(stats_before, stats_after),
                "clears_before": sum(v["clear_count"] for v in stats_before.values()),
                "clears_after": sum(v["clear_count"] for v in stats_after.values()),
                "pp_total_before": round(sum((v["pp_contribution"] or 0) for v in stats_before.values()), 3),
                "pp_total_after": round(sum((v["pp_contribution"] or 0) for v in stats_after.values()), 3),
            }
        shutil.rmtree(tmp, ignore_errors=True)
    return results


if __name__ == "__main__":
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = {"beatleader": check_bl(), "scoresaber": check_ss()}
    logs = [ln for ln in buf.getvalue().splitlines() if "外し" in ln]
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    print("\n".join(logs))
