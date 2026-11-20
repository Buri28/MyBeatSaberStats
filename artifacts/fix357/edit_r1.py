def edit(p, pairs):
    b=open(p,'rb').read().decode('utf-8'); crlf='\r\n' in b; s=b.replace('\r\n','\n')
    for a,c in pairs:
        assert s.count(a)==1,(p,a,s.count(a)); s=s.replace(a,c)
    if crlf: s=s.replace('\n','\r\n')
    open(p,'wb').write(s.encode('utf-8'))
SS='src/mybeatsaberstats/collector/scoresaber.py'; BL='src/mybeatsaberstats/collector/beatleader.py'
edit(SS, [
("""                if resp_dt.status_code == 404:
                    natural_end = api_total is not None and raw_items >= api_total
                    break""","""                if resp_dt.status_code == 404:
                    natural_end = api_total is not None and 0 < raw_items >= api_total
                    break"""),
("""                if not lbs_dt:
                    natural_end = api_total is not None and raw_items >= api_total
                    break""","""                if not lbs_dt:
                    # 1 件も読めていない（総数 0 の異常な応答など）ときは、一覧の終わりとみなさない
                    natural_end = api_total is not None and 0 < raw_items >= api_total
                    break"""),
("""    if not complete and status is not None:
        status["complete"] = False
    if not fetched:
        return 0
    scores = _load_cached_player_scores(cache_path) or {}
    scores.update(fetched)
    if prune_missing:
        if not complete or not natural_end or prior_incomplete:""","""    if not complete and status is not None:
        status["complete"] = False
    if status is not None:
        # 一覧の終わりまで読めたか（404・空ページ・ページ上限で抜けたときは False）。新ランク譜面の検知済みの印に使う
        status["ranked_scores_read_to_end"] = bool(complete and natural_end)
    if not fetched:
        return 0
    scores = _load_cached_player_scores(cache_path) or {}
    scores.update(fetched)
    if prune_missing:
        if not complete or not natural_end or prior_incomplete:"""),
("""    if local_status.get("complete") is False:
        # 途中で失敗。差し替えず、次回また検知して取り直す
        if status is not None:
            status["complete"] = False
        return len(stale)""","""    if local_status.get("complete") is False:
        # 途中で失敗。差し替えず、次回また検知して取り直す
        if status is not None:
            status["complete"] = False
        return len(stale)
    if not local_status.get("ranked_scores_read_to_end"):
        # 404・空ページ・ページ上限で途中までしか読めていない。読んでいない譜面に印を付けない（次回また検知する）
        return len(stale)"""),
])
edit(BL, [
("""                        if resp_page.status_code == 404:
                            natural_end = raw_items >= new_total
                            break""","""                        if resp_page.status_code == 404:
                            natural_end = 0 < raw_items >= new_total
                            break"""),
("""                    if not isinstance(items, list) or not items:
                        natural_end = raw_items >= new_total
                        break""","""                    if not isinstance(items, list) or not items:
                        # 1 件も読めていない（総数 0 の異常な応答など）ときは、一覧の終わりとみなさない
                        natural_end = 0 < raw_items >= new_total
                        break"""),
("""    complete = True
    hit_page_cap = False
    items_read = 0
    api_total: Optional[int] = None""","""    complete = True
    hit_page_cap = False
    ended_short = False  # 100 件未満のページで終わった
    items_read = 0
    api_total: Optional[int] = None"""),
("""        if len(items) < 100:
            break
        if page >= 300:""","""        if len(items) < 100:
            ended_short = True
            break
        if page >= 300:"""),
("""    if not complete and status is not None:
        status["complete"] = False
    if not fetched:
        return 0
    scores = _load_cached_player_scores(cache_path) or {}
    scores.update(fetched)
    if prune_missing:
        if not complete or hit_page_cap or prior_incomplete:""","""    if not complete and status is not None:
        status["complete"] = False
    if status is not None:
        # 一覧の終わりまで読めたか。100 件未満のページで終わった、または読んだ件数が総数に届いた後の空ページ。
        # 空ページ（総数に届く前）・ページ上限で抜けたときは False。新ランク譜面の検知済みの印に使う
        status["ranked_scores_read_to_end"] = bool(
            complete and not hit_page_cap
            and (ended_short or (api_total is not None and 0 < items_read >= api_total))
        )
    if not fetched:
        return 0
    scores = _load_cached_player_scores(cache_path) or {}
    scores.update(fetched)
    if prune_missing:
        if not complete or hit_page_cap or prior_incomplete:"""),
("""    if local_status.get("complete") is False:
        if status is not None:
            status["complete"] = False
        return len(stale)""","""    if local_status.get("complete") is False:
        if status is not None:
            status["complete"] = False
        return len(stale)
    if not local_status.get("ranked_scores_read_to_end"):
        # 空ページ・ページ上限で途中までしか読めていない。読んでいない譜面に印を付けない（次回また検知する）
        return len(stale)"""),
])
