p='test_ranked_sync.py'
b=open(p,'rb').read().decode('utf-8'); crlf='\r\n' in b; s=b.replace('\r\n','\n')
a='''class UnstableTailApi(FakeApi):
    """全件巡回の最後のページが欠ける（rankedDate が null の譜面の並びが不安定で、実 API は 6243 件中 6161 件しか列挙しない）。"""

    def get(self, url, params=None, timeout=None):
        r = super().get(url, params, timeout)
        if int(params["page"]) >= 4:
            r._d["leaderboards"] = []
        return r
'''
c='''class UnstableTailApi(FakeApi):
    """全件巡回で最後のページに前のページの譜面が重複して返り、その分の譜面が列挙されない
    （rankedDate が null の譜面の並びが不安定で、実 API は 6243 件中 6161 件しか列挙しない）。
    一覧そのものは最後のページ（件数が 1 ページ分未満）まで読める。"""

    def get(self, url, params=None, timeout=None):
        r = super().get(url, params, timeout)
        if int(params["page"]) >= 4:
            first = super().get(url, dict(params, page="1"), timeout)._d["leaderboards"]
            r._d["leaderboards"] = first[:len(r._d["leaderboards"])]
        return r


class EmptyMidPageApi(FakeApi):
    """全件巡回の途中のページ（3 ページ目以降）が空で返る＝一覧の途中で止まった。"""

    def get(self, url, params=None, timeout=None):
        r = super().get(url, params, timeout)
        if int(params["page"]) >= 3:
            r._d["leaderboards"] = []
        return r
'''
assert s.count(a)==1; s=s.replace(a,c)
a2='''    assert st["scoresaber_unresolved_total"] is None


# ---------------- 新ランク譜面の自分の古いプレイ ----------------
'''
c2='''    assert st["scoresaber_unresolved_total"] is None


def test_ss_full_traversal_stopped_midway_is_not_complete(tmp_path):
    """指摘 1：全件取得が途中の空ページで止まったら、キャッシュの件数が総数と合っていても完了にしない。
    ★のずれの印も消さず、status にも失敗を入れる（直す前は len(キャッシュ) == 総数 で完了扱いだった）。"""
    maps = base_maps()
    write_cache(tmp_path, maps, fetched_days_ago=1)
    mark_full(tmp_path, days_ago=20)
    st0 = state(tmp_path)
    st0["scoresaber_stars_drift"] = True
    (tmp_path / "traversal.json").write_text(json.dumps(st0), encoding="utf-8")
    status = {"complete": True}
    with mock.patch.object(ss, "CACHE_DIR", tmp_path), \
            mock.patch.object(ss, "_RANKED_FULL_TRAVERSAL_STATE", tmp_path / "traversal.json"), \
            mock.patch.object(ss, "_is_rate_limited", return_value=False):
        res = ss._get_scoresaber_leaderboards_ranked(EmptyMidPageApi(maps), fetch_until=datetime(2000, 1, 1), status=status)
    assert {str(k) for k in res} == {str(m["id"]) for m in maps}, "途中で止まったのに譜面を消した"
    st = state(tmp_path)
    assert st["scoresaber_complete"] == st0["scoresaber_complete"], "途中で止まったのに完了の記録が進んだ"
    assert st["scoresaber_last_ok"] is False and st["scoresaber_unresolved_total"] is None
    assert st.get("scoresaber_stars_drift") is True, "途中で止まったのに★のずれの印が消えた"
    assert status["complete"] is False and not status.get("ss_ranked_listing_short")


# ---------------- 新ランク譜面の自分の古いプレイ ----------------
'''
assert s.count(a2)==1; s=s.replace(a2,c2)
if crlf: s=s.replace('\n','\r\n')
open(p,'wb').write(s.encode('utf-8'))
