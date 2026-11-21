"""配布物に同梱したランク譜面一覧（ranked_seed）の取り込みのテスト。

どのテストも一時フォルダの cache / seed を使い、取得関数は擬似 API にする（実キャッシュ・実 API には触らない）。
取り込んだ後は full_traversal_due が False になり、同梱分の fetched_at から 60 日遡った差分取得になること
（全件取得なら窓の外の譜面の★が API の値に書き換わり、ページ数も増える）を確かめる。
"""
import io
import json
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent / "src"))
from mybeatsaberstats import ranked_seed as seed  # noqa: E402
from mybeatsaberstats import ranked_sync as rs  # noqa: E402
from mybeatsaberstats import updater  # noqa: E402
from mybeatsaberstats.collector import beatleader as bl  # noqa: E402
from mybeatsaberstats.collector import collector as col  # noqa: E402
from mybeatsaberstats.collector import scoresaber as ss  # noqa: E402
import test_ranked_sync as R  # noqa: E402
from test_ss_ranked_cases import NOW, FakeApi, base_maps, iso  # noqa: E402

SEED_FETCHED = iso(NOW - timedelta(days=1)).replace(".000Z", ".000000Z")
SEED_COMPLETE = (NOW - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
BL_SEED_COMPLETE = (NOW - timedelta(days=4, minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ss_payload(maps, fetched_at):
    return {"fetched_at": fetched_at, "leaderboards": {str(m["id"]): m for m in maps},
            "max_pages": 1, "total_maps": len(maps)}


def _bl_payload(maps, fetched_at):
    return {"fetched_at": fetched_at, "pages": [
        {"page": 1, "params": {"type": "Ranked"}, "data": {"metadata": {"total": len(maps)}, "data": maps}}]}


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path):
    cache = tmp_path / "cache"
    seed_dir = tmp_path / "resources" / "ranked_seed"
    cache.mkdir()
    _write(seed_dir / "scoresaber_ranked_maps.json", _ss_payload(base_maps(), SEED_FETCHED))
    _write(seed_dir / "beatleader_ranked_maps.json", _bl_payload(R.bl_base(), SEED_FETCHED))
    _write(seed_dir / "ranked_full_traversal.json", {
        "scoresaber": SEED_COMPLETE, "scoresaber_complete": SEED_COMPLETE, "scoresaber_last_ok": True,
        "scoresaber_unresolved_total": None, "scoresaber_unresolved_count": 0,
        "beatleader": BL_SEED_COMPLETE, "beatleader_complete": BL_SEED_COMPLETE, "beatleader_last_ok": True,
        "beatleader_unresolved_total": None, "beatleader_unresolved_count": 0,
    })
    _write(seed_dir / "scoresaber_reweights.json", {"index_checked_at": SEED_COMPLETE, "processed": {"x": {}}})
    return tmp_path, cache, seed_dir


def _import(env):
    tmp, cache, seed_dir = env
    return seed.import_ranked_seed(cache, seed_dir, log_dir=tmp / "logs")


def _traversal(cache):
    return _read(cache / "ranked_full_traversal.json")


def _ss_fetch(cache):
    """collector と同じく、一覧の fetched_at から 60 日遡った窓で SS の一覧を取る。API の 110 番だけ★が違う。"""
    api = FakeApi([dict(m, stars=7.5) if m["id"] == 110 else m for m in base_maps()])
    fetched = col._read_cache_fetched_at(cache / "scoresaber_ranked_maps.json")
    assert fetched is not None
    with mock.patch.object(ss, "CACHE_DIR", cache), \
            mock.patch.object(ss, "_RANKED_FULL_TRAVERSAL_STATE", cache / "ranked_full_traversal.json"), \
            mock.patch.object(ss, "_is_rate_limited", return_value=False):
        ss._get_scoresaber_leaderboards_ranked(api, fetch_until=fetched - timedelta(days=60))
    return api.calls, _read(cache / "scoresaber_ranked_maps.json")["leaderboards"]["110"]["stars"]


def _bl_fetch(cache):
    maps = [json.loads(json.dumps(m)) for m in R.bl_base()]
    maps[10]["difficulty"]["stars"] = 8.0  # 窓の外（200 日前）
    lid = maps[10]["id"]
    api = R.BlFake(maps)
    fetched = col._read_cache_fetched_at(cache / "beatleader_ranked_maps.json")
    assert fetched is not None
    with mock.patch.object(bl, "CACHE_DIR", cache), \
            mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", cache / "ranked_full_traversal.json"):
        res = bl._get_beatleader_leaderboards_ranked(api, fetch_until=fetched - timedelta(days=60))
    return api.calls, {x["id"]: x for x in res}[lid]["difficulty"]["stars"]


def _assert_diff_only(cache):
    """全件取得にならず、同梱分の fetched_at からの差分取得になったこと。"""
    path = cache / "ranked_full_traversal.json"
    assert not rs.full_traversal_due(path, "scoresaber")
    assert not rs.full_traversal_due(path, "beatleader")
    assert col._read_cache_fetched_at(cache / "scoresaber_ranked_maps.json") == datetime(2026, 11, 17)
    calls, stars = _ss_fetch(cache)
    assert stars == 5.0, "SS が全件取得した（窓の外の★が書き換わった）"
    assert calls <= 2, f"SS のページ数が多い calls={calls}（全件なら 4）"
    calls, stars = _bl_fetch(cache)
    assert stars == 5.0, "BL が全件取得した（窓の外の★が書き換わった）"
    assert calls <= 1, f"BL のページ数が多い calls={calls}（全件なら 4）"


# ---------------- 取り込みの条件 ----------------

def test_new_user_empty_cache(env):
    tmp, cache, seed_dir = env
    assert _import(env) == {"scoresaber": "imported", "beatleader": "imported"}
    st = _traversal(cache)
    assert st["scoresaber_complete"] == SEED_COMPLETE and st["beatleader_complete"] == BL_SEED_COMPLETE
    assert st["scoresaber"] == SEED_COMPLETE and st["beatleader"] == BL_SEED_COMPLETE
    assert st["scoresaber_last_ok"] is True and st["beatleader_last_ok"] is True
    assert st["scoresaber_seed_imported"] == SEED_FETCHED
    assert _read(cache / "scoresaber_reweights.json") == _read(seed_dir / "scoresaber_reweights.json")
    assert "取り込みました" in (tmp / "logs" / seed.LOG_FILE_NAME).read_text(encoding="utf-8")
    _assert_diff_only(cache)


def test_update_from_v086_without_complete(env):
    """v0.8.6 の利用者：一覧はあるが古く、ranked_full_traversal.json に _complete が無い。"""
    tmp, cache, _ = env
    old = iso(NOW - timedelta(days=40))
    _write(cache / "scoresaber_ranked_maps.json", _ss_payload(base_maps()[:20], old))
    _write(cache / "beatleader_ranked_maps.json", _bl_payload(R.bl_base()[:50], old))
    _write(cache / "ranked_full_traversal.json", {"scoresaber": {"123": {"a": "b"}}})  # 旧版の別の記録
    assert _import(env) == {"scoresaber": "imported", "beatleader": "imported"}
    assert len(_read(cache / "scoresaber_ranked_maps.json")["leaderboards"]) == len(base_maps())
    _assert_diff_only(cache)


def test_newer_user_list_without_complete_is_replaced(env):
    """利用者の一覧が新しくても、全件取得を終えた記録が無ければ同梱分にする（そのままだと全件取得になる）。"""
    _, cache, _ = env
    newer = iso(NOW)
    _write(cache / "scoresaber_ranked_maps.json", _ss_payload(base_maps()[:20], newer))
    _write(cache / "beatleader_ranked_maps.json", _bl_payload(R.bl_base()[:50], newer))
    assert _import(env) == {"scoresaber": "imported", "beatleader": "imported"}
    _assert_diff_only(cache)


def test_user_newer_is_untouched(env):
    _, cache, _ = env
    newer = iso(NOW)
    user_state = {"scoresaber": "2026-11-18T00:00:00Z", "scoresaber_complete": "2026-11-18T00:00:00Z",
                  "beatleader": "2026-11-18T00:00:00Z", "beatleader_complete": "2026-11-18T00:00:00Z"}
    _write(cache / "scoresaber_ranked_maps.json", _ss_payload(base_maps()[:20], newer))
    _write(cache / "beatleader_ranked_maps.json", _bl_payload(R.bl_base()[:50], newer))
    _write(cache / "ranked_full_traversal.json", user_state)
    before = {p.name: p.read_bytes() for p in cache.iterdir()}
    assert _import(env) == {"scoresaber": "user_newer", "beatleader": "user_newer"}
    assert {p.name: p.read_bytes() for p in cache.iterdir()} == before
    assert not (cache / "scoresaber_reweights.json").exists()


def test_older_user_list_with_complete_is_replaced(env):
    _, cache, _ = env
    old = iso(NOW - timedelta(days=30))
    _write(cache / "scoresaber_ranked_maps.json", _ss_payload(base_maps()[:20], old))
    _write(cache / "beatleader_ranked_maps.json", _bl_payload(R.bl_base()[:50], old))
    _write(cache / "ranked_full_traversal.json", {"scoresaber_complete": "2026-10-01T00:00:00Z",
                                                  "beatleader_complete": "2026-10-01T00:00:00Z"})
    _write(cache / "scoresaber_reweights.json", {"processed": {"mine": {}}})
    assert _import(env) == {"scoresaber": "imported", "beatleader": "imported"}
    assert _read(cache / "scoresaber_reweights.json") == {"processed": {"mine": {}}}, "利用者のリウェイトを上書きした"
    _assert_diff_only(cache)


@pytest.mark.parametrize("service", ["scoresaber", "beatleader"])
def test_user_star_drift_is_kept(env, service):
    _, cache, _ = env
    _write(cache / "ranked_full_traversal.json", {f"{service}_stars_drift": True})
    assert _import(env)[service] == "imported"
    st = _traversal(cache)
    assert st[f"{service}_stars_drift"] is True
    assert rs.full_traversal_due(cache / "ranked_full_traversal.json", service), "★ずれの印があるのに全件取得しない"


def test_second_import_does_nothing(env):
    _, cache, _ = env
    assert _import(env) == {"scoresaber": "imported", "beatleader": "imported"}
    before = {p.name: p.read_bytes() for p in cache.iterdir()}
    assert _import(env) == {"scoresaber": "already_imported", "beatleader": "already_imported"}
    assert {p.name: p.read_bytes() for p in cache.iterdir()} == before


def test_reimport_when_list_deleted_after_import(env):
    """取り込んだ後に一覧が消えたら、全件取得にならないよう同じ同梱分を取り込み直す。"""
    _, cache, _ = env
    assert _import(env)["scoresaber"] == "imported"
    (cache / "scoresaber_ranked_maps.json").unlink()
    assert _import(env) == {"scoresaber": "imported", "beatleader": "already_imported"}
    _assert_diff_only(cache)


def test_unresolved_mismatch_follows_seed(env):
    """総数の食い違いの記録は試行日時と組なので、同梱分の値にそろえる。"""
    _, cache, _ = env
    _write(cache / "ranked_full_traversal.json", {"scoresaber_unresolved_total": 99, "scoresaber_unresolved_count": 2})
    _import(env)
    st = _traversal(cache)
    assert st["scoresaber_unresolved_total"] is None and st["scoresaber_unresolved_count"] == 0


def test_reweights_copy_failure_keeps_import(env):
    tmp, cache, _ = env
    real = seed.atomic_write_text

    def _fail_reweights(path, text, *a, **k):
        if Path(path).name == seed.REWEIGHTS_FILE_NAME:
            raise OSError("locked")
        return real(path, text, *a, **k)

    with mock.patch.object(seed, "atomic_write_text", side_effect=_fail_reweights):
        assert _import(env)["scoresaber"] == "imported"
    assert "locked" in (tmp / "logs" / seed.LOG_FILE_NAME).read_text(encoding="utf-8")
    _assert_diff_only(cache)


def test_default_seed_dir_falls_back_to_meipass(tmp_path):
    """frozen 版で exe の隣に resources があっても、_internal 側の ranked_seed を見つける。"""
    exe_res = tmp_path / "resources"
    exe_res.mkdir()
    internal_seed = tmp_path / "_internal" / "resources" / seed.SEED_DIR_NAME
    internal_seed.mkdir(parents=True)
    with mock.patch.object(seed, "RESOURCES_DIR", exe_res),             mock.patch.object(seed.sys, "frozen", True, create=True),             mock.patch.object(seed.sys, "_MEIPASS", str(tmp_path / "_internal"), create=True):
        assert seed.default_seed_dir() == internal_seed
    with mock.patch.object(seed, "RESOURCES_DIR", exe_res):
        assert seed.default_seed_dir() == exe_res / seed.SEED_DIR_NAME


@pytest.mark.parametrize("user_fetched", ["2026-11-17T00:00:00", "2026-11-17T09:00:00+09:00", "2026-11-17T00:00:00.000000Z"])
def test_same_time_in_other_formats_is_not_older(env, user_fetched):
    """fetched_at の書き方（Z 無し・+09:00・マイクロ秒）が違っても、同じ時刻なら利用者の方を残す。"""
    _, cache, _ = env
    _write(cache / "scoresaber_ranked_maps.json", _ss_payload(base_maps()[:20], user_fetched))
    _write(cache / "ranked_full_traversal.json", {"scoresaber_complete": "2026-11-10T00:00:00Z"})
    assert _import(env)["scoresaber"] == "user_newer"


def test_traversal_write_failure_leaves_retryable_state(env):
    """一覧を書いた後に記録の書き込みが失敗しても、次回は取り込み直せる（印が付いていない）。"""
    _, cache, _ = env
    real = seed.atomic_write_text

    def _fail_traversal(path, text, *a, **k):
        if Path(path).name == seed.TRAVERSAL_FILE_NAME:
            raise OSError("traversal locked")
        return real(path, text, *a, **k)

    with mock.patch.object(seed, "atomic_write_text", side_effect=_fail_traversal):
        assert _import(env)["scoresaber"] == "failed"
    assert _import(env)["scoresaber"] == "imported"
    _assert_diff_only(cache)


def test_no_seed_dir_does_nothing(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    assert seed.import_ranked_seed(cache, tmp_path / "missing", log_dir=tmp_path / "logs") == {}
    assert list(cache.iterdir()) == []
    assert rs.full_traversal_due(cache / "ranked_full_traversal.json", "scoresaber")


@pytest.mark.parametrize("breakage", ["traversal_json", "list_json", "no_complete", "list_missing", "no_fetched_at"])
def test_broken_seed_keeps_old_behavior(env, breakage):
    _, cache, seed_dir = env
    if breakage == "traversal_json":
        (seed_dir / "ranked_full_traversal.json").write_text("{broken", encoding="utf-8")
    elif breakage == "list_json":
        (seed_dir / "scoresaber_ranked_maps.json").write_text('{"fetched_at": "2026-11-17T00:00:00Z", "leader', encoding="utf-8")
    elif breakage == "no_complete":
        _write(seed_dir / "ranked_full_traversal.json", {"beatleader_complete": BL_SEED_COMPLETE})
    elif breakage == "list_missing":
        (seed_dir / "scoresaber_ranked_maps.json").unlink()
    elif breakage == "no_fetched_at":
        _write(seed_dir / "scoresaber_ranked_maps.json", {"leaderboards": {"1": {}}})
    result = _import(env)
    assert result["scoresaber"] != "imported"
    assert not (cache / "scoresaber_ranked_maps.json").exists()
    assert rs.full_traversal_due(cache / "ranked_full_traversal.json", "scoresaber"), "今までどおり全件取得にならない"
    assert not (cache / "scoresaber_reweights.json").exists()


def test_cancel_is_not_swallowed(env):
    with mock.patch.object(seed, "_import_service", side_effect=RuntimeError("SNAPSHOT_CANCELLED")):
        with pytest.raises(RuntimeError, match="SNAPSHOT_CANCELLED"):
            _import(env)


def test_other_failure_is_logged_and_continues(env):
    tmp, _, _ = env
    with mock.patch.object(seed, "atomic_write_text", side_effect=OSError("disk full")):
        assert _import(env) == {"scoresaber": "failed", "beatleader": "failed"}
    assert "disk full" in (tmp / "logs" / seed.LOG_FILE_NAME).read_text(encoding="utf-8")


# ---------------- Snapshot での呼び出し順 ----------------

def test_collector_imports_before_ranked_fetch_and_reweights(tmp_path):
    order = []

    def _ss_fetch_stub(*a, **k):
        order.append("ss_ranked")
        raise RuntimeError("SNAPSHOT_CANCELLED")

    with mock.patch.object(col, "CACHE_DIR", tmp_path), \
            mock.patch.object(col, "import_ranked_seed", side_effect=lambda *a, **k: order.append("seed")), \
            mock.patch.object(col, "_load_beatleader_ranked_ids", side_effect=lambda *a: order.append("bl_ids") or set()), \
            mock.patch.object(col, "_load_scoresaber_ranked_ids", side_effect=lambda *a: order.append("ss_ids") or set()), \
            mock.patch.object(col, "_get_beatleader_leaderboards_ranked", side_effect=lambda *a, **k: order.append("bl_ranked")), \
            mock.patch.object(col, "apply_scoresaber_reweights", side_effect=lambda *a, **k: order.append("reweights")), \
            mock.patch.object(col, "_get_scoresaber_leaderboards_ranked", side_effect=_ss_fetch_stub):
        with pytest.raises(RuntimeError, match="SNAPSHOT_CANCELLED"):
            col._create_snapshot_for_steam_id("76561198000000000", session=mock.Mock())
        col._BackgroundFetch.cancel_all()
    assert order[0] == "seed", order
    assert {"bl_ids", "ss_ids", "bl_ranked"} <= set(order), order  # 取り込み前の一覧で新規 ranked を判定しない
    assert order.index("reweights") < order.index("ss_ranked")


def test_collector_cancel_in_seed_import_propagates(tmp_path):
    with mock.patch.object(col, "CACHE_DIR", tmp_path), \
            mock.patch.object(col, "import_ranked_seed", side_effect=RuntimeError("SNAPSHOT_CANCELLED")), \
            mock.patch.object(col, "_get_beatleader_leaderboards_ranked") as bl_fetch:
        with pytest.raises(RuntimeError, match="SNAPSHOT_CANCELLED"):
            col._create_snapshot_for_steam_id("76561198000000000", session=mock.Mock())
    bl_fetch.assert_not_called()


def test_mapper_refresh_imports_seed_first(tmp_path):
    from mybeatsaberstats import beatleader_mapper_cache as mc
    order = []
    with mock.patch.object(mc, "_CACHE_DIR", tmp_path), \
            mock.patch.object(mc, "import_ranked_seed", side_effect=lambda *a, **k: order.append(("seed", a[0]))), \
            mock.patch.object(mc, "make_session", return_value=mock.Mock()), \
            mock.patch.object(mc, "_get_beatleader_leaderboards_ranked", side_effect=RuntimeError("stop")):
        with pytest.raises(RuntimeError, match="stop"):
            mc.refresh_bl_mapper_played_cache("76561198000000000", "since")
    assert order == [("seed", tmp_path)]


# ---------------- アップデーターが ranked_seed をサブフォルダごと入れ替えるか ----------------

def _release_tree(root: Path, seed_name: str) -> Path:
    rel = root / "MyBeatSaberStats"
    _write(rel / "_internal" / "resources" / "ranked_seed" / seed_name, {"new": True})
    _write(rel / "_internal" / "resources" / "app_icon.png", {"icon": True})
    (rel / "MyBeatSaberStats.exe").write_bytes(b"exe")
    return rel


def test_staged_update_mirrors_ranked_seed(tmp_path):
    """frozen 版の更新（zip 全体のミラー）：ranked_seed の中身が入れ替わり、利用者の cache は残る。"""
    src = _release_tree(tmp_path / "zip", "scoresaber_ranked_maps.json")
    install = tmp_path / "install"
    _write(install / "_internal" / "resources" / "ranked_seed" / "old_seed.json", {"old": True})
    _write(install / "cache" / "scoresaber_ranked_maps.json", {"user": True})
    updater._mirror_directory(src, install)
    seed_dir = install / "_internal" / "resources" / "ranked_seed"
    assert _read(seed_dir / "scoresaber_ranked_maps.json") == {"new": True}
    assert not (seed_dir / "old_seed.json").exists()
    assert _read(install / "cache" / "scoresaber_ranked_maps.json") == {"user": True}


def test_inplace_internal_sync_includes_ranked_seed(tmp_path):
    """frozen 版のその場の同期（_internal/resources を zip に合わせる）も ranked_seed を含む。"""
    src = _release_tree(tmp_path / "zip", "beatleader_ranked_maps.json")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in src.rglob("*"):
            if p.is_file():
                zf.write(p, "MyBeatSaberStats/" + p.relative_to(src).as_posix())
    internal = tmp_path / "install" / "_internal"
    _write(internal / "resources" / "ranked_seed" / "old_seed.json", {"old": True})
    resp = mock.Mock(content=buf.getvalue())
    with mock.patch.object(updater, "_internal_dir", return_value=internal), \
            mock.patch.object(updater.requests, "get", return_value=resp), \
            mock.patch.object(updater, "_verify_release_bytes"):
        updater._sync_internal_files_from_release_zip("https://example.invalid/r.zip", {"resources"}, None, 1)
    seed_dir = internal / "resources" / "ranked_seed"
    assert _read(seed_dir / "beatleader_ranked_maps.json") == {"new": True}
    assert not (seed_dir / "old_seed.json").exists()
