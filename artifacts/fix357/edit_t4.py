def edit(p, pairs):
    b=open(p,'rb').read().decode('utf-8'); crlf='\r\n' in b; s=b.replace('\r\n','\n')
    for a,c in pairs:
        assert s.count(a)==1,(p,a,s.count(a)); s=s.replace(a,c)
    if crlf: s=s.replace('\n','\r\n')
    open(p,'wb').write(s.encode('utf-8'))
edit('test_ranked_sync.py', [
('''def test_bl_long_gap(tmp_path):''','''class BlStatusFake(BlFake):
    """指定ページから 404 または空ページを返す。"""

    def __init__(self, maps, stop_page, mode):
        super().__init__(maps)
        self.stop_page, self.mode = stop_page, mode

    def get(self, url, params=None, timeout=None):
        page = int(params["page"])
        if page >= self.stop_page:
            self.calls += 1
            if self.mode == 404:
                r = T._Resp({})
                r.status_code = 404
                return r
            return T._Resp({"metadata": {"total": len(self.maps)}, "data": []})
        return super().get(url, params, timeout)


def _bl_full(tmp, api, status):
    with mock.patch.object(bl, "CACHE_DIR", tmp), \
            mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", tmp / "traversal.json"), \
            mock.patch.object(bl, "_log_api_failure"):
        return bl._get_beatleader_leaderboards_ranked(api, fetch_until=datetime(2000, 1, 1), status=status)


def test_bl_full_traversal_stopped_midway_is_not_complete(tmp_path):
    """指摘 1：全件取得が途中の 404・空ページで止まったら、キャッシュの件数が総数と合っていても完了にしない。
    ★のずれの印も消さない（直す前は len(キャッシュ) == 総数 で完了扱いだった）。"""
    for mode in (404, 200):
        d = tmp_path / str(mode)
        d.mkdir()
        maps = bl_base()
        bl_write(d, maps, 1)
        bl_mark(d, 20)
        st0 = state(d)
        st0["beatleader_stars_drift"] = True
        (d / "traversal.json").write_text(json.dumps(st0), encoding="utf-8")
        status = {"complete": True}
        got = _bl_full(d, BlStatusFake(maps, 3, mode), status)
        assert {x["id"] for x in got} == {m["id"] for m in maps}, (mode, "途中で止まったのに譜面を消した")
        st = state(d)
        assert st["beatleader_complete"] == st0["beatleader_complete"], (mode, "完了の記録が進んだ")
        assert st["beatleader_last_ok"] is False, mode
        assert st.get("beatleader_stars_drift") is True, (mode, "★のずれの印が消えた")
        assert status["complete"] is False, mode


def test_bl_full_traversal_exact_multiple_ends_with_empty_page(tmp_path):
    """総数がページ数でちょうど割り切れ、最後の次のページが空・404 で返るのは一覧の終わり（完了）。"""
    for mode in (404, 200):
        d = tmp_path / str(mode)
        d.mkdir()
        maps = [bl_lb(i, 10 + i) for i in range(200)]
        bl_write(d, maps, 1)
        bl_mark(d, 20)
        status = {"complete": True}
        _bl_full(d, BlStatusFake(maps, 3, mode), status)
        st = state(d)
        assert st["beatleader_complete"] == st["beatleader"] and st["beatleader_last_ok"] is True, mode
        assert status["complete"] is True, mode


def test_bl_first_page_404_marks_incomplete(tmp_path):
    """指摘 6：キャッシュを使う経路で 1 ページ目が 404 なら status["complete"] = False を入れる。"""
    for until in (None, BL_NOW - timedelta(days=61), datetime(2000, 1, 1)):
        d = tmp_path / str(len(list(tmp_path.iterdir())))
        d.mkdir()
        maps = bl_base()
        bl_write(d, maps, 1)
        bl_mark(d, 1)
        status = {"complete": True}
        with mock.patch.object(bl, "CACHE_DIR", d), \
                mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", d / "traversal.json"):
            got = bl._get_beatleader_leaderboards_ranked(BlStatusFake(maps, 1, 404), fetch_until=until, status=status)
        assert len(got) == len(maps), until
        assert status["complete"] is False, until


def test_bl_first_page_404_keeps_scores_of_unranked_map(tmp_path):
    """指摘 6 の影響：1 ページ目 404 の後のスコアの取り直しで、ランクから外れたばかりの譜面のスコアを外さない。"""
    ranked = [bl_lb(i, 10 + i) for i in range(5)]
    bl_write(tmp_path, ranked, 1)
    bl_mark(tmp_path, 1)

    def item(i, pp):
        return {"pp": pp, "timeset": "1700000000",
                "leaderboard": {"id": f"L{i}", "difficulty": {"status": 3, "rankedTime": 1}}}
    scores = {f"L{i}": item(i, 100.0 - i) for i in range(5)}
    (tmp_path / "beatleader_player_scores_7.json").write_text(
        json.dumps({"fetched_at": iso(BL_NOW), "total_play_count": 5, "scores": scores}), encoding="utf-8")

    class ScoreApi:
        def get(self, url, params=None, timeout=None):
            data = [item(i, 100.0 - i) for i in range(4)] if params["page"] == "1" else []  # L4 はランク外れで返らない
            return T._Resp({"metadata": {"total": 4}, "data": data})
    status = {"complete": True}
    with mock.patch.object(bl, "CACHE_DIR", tmp_path), \
            mock.patch.object(bl, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"):
        bl._get_beatleader_leaderboards_ranked(BlStatusFake(ranked, 1, 404), fetch_until=datetime(2000, 1, 1),
                                               status=status)
        bl.refresh_beatleader_ranked_scores("7", ScoreApi(), status=status)
    saved = json.loads((tmp_path / "beatleader_player_scores_7.json").read_text(encoding="utf-8"))["scores"]
    assert "L4" in saved, "1 ページ目 404 なのにスコアを外した"


def test_bl_long_gap(tmp_path):'''),
])
