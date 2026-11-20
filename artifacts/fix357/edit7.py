def edit(p, pairs):
    b=open(p,'rb').read().decode('utf-8'); crlf='\r\n' in b; s=b.replace('\r\n','\n')
    for a,c in pairs:
        assert s.count(a)==1,(p,a,s.count(a)); s=s.replace(a,c)
    if crlf: s=s.replace('\n','\r\n')
    open(p,'wb').write(s.encode('utf-8'))

A='src/mybeatsaberstats/accsaber_reloaded.py'
edit(A, [
("""_USER_PROFILE_LOCK = threading.Lock()
""","""_USER_PROFILE_LOCK = threading.Lock()
#: /users/{id} が 404（AccSaber に登録していないプレイヤー）だった ID。API の失敗と分けるために覚える。
_USER_NOT_FOUND: "WeakKeyDictionary[requests.Session, set]" = WeakKeyDictionary()


def user_not_registered(player_id: str, session: requests.Session) -> bool:
    \"\"\"このセッションで /users/{id} が 404 だった（AccSaber に登録していない）か。\"\"\"
    with _USER_PROFILE_LOCK:
        try:
            return str(player_id) in _USER_NOT_FOUND.get(session, set())
        except TypeError:
            return False
"""),
("""        resp = session.get(f"{BASE_URL}/users/{player_id}", timeout=30)
        if _is_rate_limited(resp, "fetch_user_profile", f"player_id={player_id}"):
            return None
        resp.raise_for_status()""","""        resp = session.get(f"{BASE_URL}/users/{player_id}", timeout=30)
        if _is_rate_limited(resp, "fetch_user_profile", f"player_id={player_id}"):
            return None
        if resp.status_code == 404:
            # 登録していないプレイヤー。API の失敗ではないので失敗の記録には残さない
            try:
                with _USER_PROFILE_LOCK:
                    _USER_NOT_FOUND.setdefault(session, set()).add(str(player_id))
            except TypeError:
                pass
            return None
        resp.raise_for_status()"""),
("""    *, on_warning=None,
) -> Dict[str, Optional[AccSaberReloadedPlayer]]:
    \"\"\"指定プレイヤーの AccSaber Reloaded 全カテゴリのランク情報を取得する。

    戻り値: {"overall": ..., "true": ..., "standard": ..., "tech": ...}
    各カテゴリで見つからない / API エラーの場合は None。
    \"\"\"""","""    *, on_warning=None,
    failed: Optional[set] = None,
) -> Dict[str, Optional[AccSaberReloadedPlayer]]:
    \"\"\"指定プレイヤーの AccSaber Reloaded 全カテゴリのランク情報を取得する。

    戻り値: {"overall": ..., "true": ..., "standard": ..., "tech": ...}
    各カテゴリで見つからない / API エラーの場合は None。
    failed に set を渡すと、API エラーだったカテゴリを入れる（None でも failed に無ければ、そのカテゴリに未登録）。
    \"\"\""""),
("""            if on_warning is not None:
                on_warning(f"AccSaber {category}: {exc}")
            result[category] = None
""","""            if on_warning is not None:
                on_warning(f"AccSaber {category}: {exc}")
            if failed is not None:
                failed.add(category)
            result[category] = None
"""),
])

C='src/mybeatsaberstats/collector/collector.py'
edit(C, [
("""from ..accsaber_reloaded import compute_effective_played_counts_from_cache as _compute_rl_effective_played_counts
""","""from ..accsaber_reloaded import compute_effective_played_counts_from_cache as _compute_rl_effective_played_counts
from ..accsaber_reloaded import user_not_registered as _rl_user_not_registered
"""),
("""    def _rl_warning(message: str) -> None:
        accsaber_fetch_errors.append(message)
        _add_warning(message)
""","""    def _rl_warning(message: str) -> None:
        # 同じ警告は 1 回だけ（カテゴリの例外は fetch 側の on_warning で出しているので重ねない）
        if message in accsaber_fetch_errors:
            return
        accsaber_fetch_errors.append(message)
        _add_warning(message)
"""),
("""        _rl_country = (scoresaber_country or "").upper() or None
        try:
            _rl_result = _fetch_accsaber_reloaded(_rl_player_id, country=_rl_country, session=session,
                                               on_warning=_rl_warning)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            _rl_warning(f"AccSaber: {exc}")
            _rl_result = {}

        for cat in ("overall", "true", "standard", "tech"):
            if _rl_result.get(cat) is None:
                _rl_warning(f"AccSaber {cat}: ランキング未取得（API失敗またはプレイヤー未登録）。")
""","""        _rl_country = (scoresaber_country or "").upper() or None
        # API の失敗（例外）だったカテゴリ。None でもここに無ければ、そのカテゴリに登録していないだけ（警告しない）
        _rl_failed_cats: set[str] = set()
        _rl_call_failed = False
        try:
            _rl_result = _fetch_accsaber_reloaded(_rl_player_id, country=_rl_country, session=session,
                                               on_warning=_rl_warning, failed=_rl_failed_cats)
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            _rl_warning(f"AccSaber: {exc}")
            _rl_result = {}
            _rl_call_failed = True

        _rl_unregistered_cats = [
            cat for cat in ("overall", "true", "standard", "tech")
            if _rl_result.get(cat) is None and cat not in _rl_failed_cats and not _rl_call_failed
        ]
        if _rl_unregistered_cats:
            print(f"AccSaber: 未登録のカテゴリ {', '.join(_rl_unregistered_cats)}（警告は出しません）")
        # /users/{id} が 404＝AccSaber に登録していない。XP・マイルストーン等が無いのは失敗ではない
        _rl_not_registered = _rl_user_not_registered(_rl_player_id, session)
"""),
("""        if _rl_xp_result is None:
            _rl_warning("AccSaber XP: 未取得（API失敗またはプレイヤー未登録）。")
""","""        if _rl_xp_result is None and not _rl_not_registered:
            _rl_warning("AccSaber XP: 未取得。")
"""),
("""        if _rl_milestones is None:
            _rl_warning("AccSaber Milestones: 未取得。")
""","""        if _rl_milestones is None and not _rl_not_registered:
            _rl_warning("AccSaber Milestones: 未取得。")
"""),
("""        for cat in ("overall", "true", "standard", "tech"):
            if _rl_skill.get(cat) is None:
                _rl_warning(f"AccSaber Skill Level {cat}: 未取得。")
""","""        for cat in ("overall", "true", "standard", "tech"):
            # 登録していないカテゴリのスキルレベルは無くて当然なので警告しない
            if _rl_skill.get(cat) is None and cat not in _rl_unregistered_cats and not _rl_not_registered:
                _rl_warning(f"AccSaber Skill Level {cat}: 未取得。")
"""),
("""        if accsaber_fetch_errors:
            accsaber_fetch_status = "partial" if any(_rl_result.values()) else "failed"
""","""        if accsaber_fetch_errors:
            accsaber_fetch_status = "partial" if any(_rl_result.values()) else "failed"
        elif len(_rl_unregistered_cats) == 4:
            # どのカテゴリにも登録していない（API は成功）。失敗ではない
            accsaber_fetch_status = "not_registered"
"""),
])

edit('src/mybeatsaberstats/accsaber_status.py', [
("""              "failed": "取得失敗", "partial": "一部未取得"}""","""              "failed": "取得失敗", "partial": "一部未取得", "not_registered": "未登録"}"""),
])
