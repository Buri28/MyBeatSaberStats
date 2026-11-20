def edit(p, pairs):
    b=open(p,'rb').read().decode('utf-8'); crlf='\r\n' in b; s=b.replace('\r\n','\n')
    for a,c in pairs:
        assert s.count(a)==1,(p,a,s.count(a)); s=s.replace(a,c)
    if crlf: s=s.replace('\n','\r\n')
    open(p,'wb').write(s.encode('utf-8'))
edit('test_prune_missing_scores.py', [
('''def ss_refresh(tmp, api, **kw):
    with mock.patch.object(ss, "CACHE_DIR", tmp), mock.patch.object(ss, "log_api_failure"):
        return ss.refresh_scoresaber_ranked_scores("1", api, **kw)
''','''def ss_refresh(tmp, api, **kw):
    # プロフィールの rankedPlayCount（サーバー上の pp 付きスコアの数）。指定が無ければ API が返す件数にする
    if "ranked_play_count" not in kw and isinstance(getattr(api, "items", None), list):
        kw["ranked_play_count"] = sum(1 for x in api.items if ss._ss_score_pp(x) > 0)
    with mock.patch.object(ss, "CACHE_DIR", tmp), mock.patch.object(ss, "log_api_failure"):
        return ss.refresh_scoresaber_ranked_scores("1", api, **kw)
'''),
('''    st = {"complete": True}
    fetch(tmp_path, ShortApi(maps), st)
    assert st["complete"] is False
    assert st.get("ss_ranked_listing_short") is True''','''    st = {"complete": True}
    fetch(tmp_path, ShortApi(maps), st)
    # 指摘 2：一覧を最後まで読めて件数だけ足りないのは失敗ではない（complete は落とさない）
    assert st["complete"] is True
    assert st.get("ss_ranked_listing_short") is True'''),
])
