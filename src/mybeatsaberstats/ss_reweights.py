"""ScoreSaber の月例リウェイト（wiki の月ごとのページ）で、SS ランク譜面キャッシュの★を更新する。

ランク譜面一覧の差分取得は rankedDate が前回の 60 日前以降の譜面しか読まないため、それより古い譜面の★が
リウェイトで変わっても、プレイしたスコアで★のずれが見えない限り気づけない（★統計の分母が古いまま残る）。
そこで、Snapshot の SS ランク譜面一覧の取得の直前に、次の 1 段を足す。

1. wiki の一覧ページ（REWEIGHTS_INDEX_URL）を読むのは 1 日 1 回まで（INDEX_INTERVAL_HOURS）。読もうとした日時を記録する。
2. 未処理の新しい月のページだけ、古い順に読む。全件取得を最後まで終えた日時（ranked_full_traversal.json の
   scoresaber_complete）より前のリウェイトは、全件取得で既に入っているので対象にしない
   （遅れて出るページと見出しの日付のタイムゾーンのため、リンクは前月から読み、見出しの日付は 1 日の余裕を持って比べる）。
3. 各行（曲名 | 難易度 | 譜面作者 | ⭐ 旧 → ⭐ 新）を、曲名・難易度・譜面作者でキャッシュと突き合わせる。
   - キャッシュの★が「新」と同じ：何もしない（反映済み）。
   - 「旧」と同じ：その譜面だけ SS API（leaderboard/by-hash/{hash}/info。playlist_view と同じ使い方）で取り直し、★を書き換える。
     返った★がまだ「旧」なら（SS に未反映）書き換えず、そのページは処理済みにしないで翌日以降に読み直す。
   - 1 つに決まらない・見つからない・★がどちらとも合わない・全体リウェイトのページ（リンク名か見出しに Global）：
     ranked_sync.flag_star_drift で印を付けるだけ（全件の取り直しは今の仕組みに任せる）。
4. 処理し終えたページを STATE_FILE_NAME に記録し、二度処理しない。

失敗（通信・404・HTML の形の変化など）したら何もせずにログを 1 行出して戻る。読み取れなかったページは処理済みにしない
（読み直しは一覧ページと同じく 1 日 1 回まで）。
"""
from __future__ import annotations

import html as _html
import json
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional

from . import ranked_sync
from .fileio import atomic_write_text

WIKI_BASE_URL = "https://wiki.scoresaber.com"
REWEIGHTS_INDEX_URL = f"{WIKI_BASE_URL}/ranking/reweights.html"
SCORESABER_LEADERBOARD_INFO_BY_HASH_URL = "https://scoresaber.com/api/leaderboard/by-hash/{song_hash}/info"
#: 処理済みのページと、一覧ページを読んだ日時の記録（cache フォルダに置く）。
STATE_FILE_NAME = "scoresaber_reweights.json"
#: 一覧ページを読み直すまでの時間（1 日 1 回まで）。
INDEX_INTERVAL_HOURS = 24

_MONTHS = {
    name: i for i, name in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1)
}
# wiki の難易度表記 → ScoreSaber の difficulty 番号（キャッシュの difficulty.difficulty）
_DIFFICULTY_NUMBERS = {"easy": 1, "normal": 3, "hard": 5, "expert": 7, "expert+": 9, "expertplus": 9}

_PAGE_LINK_RE = re.compile(r'href="(/ranking/reweights/(\d{4})/([A-Za-z]+(?:-[A-Za-z]+)*-(\d{4}))(?:\.html)?)"')
_HEADING_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
_STAR_CHANGE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*→\s*(?:⭐\s*)?([0-9]+(?:\.[0-9]+)?)")
_EXPECTED_HEADER = ["map name", "difficulty", "mapper(s)", "star rating change"]


class ReweightParseError(ValueError):
    """wiki の HTML が想定した形でない。"""


# ---------------------------------------------------------------- HTML の読み取り

def parse_index(text: str) -> list[dict]:
    """一覧ページから月ごとのページへのリンクを取り出す（重複は 1 つにまとめ、古い順に並べる）。"""
    pages: dict[str, dict] = {}
    for m in _PAGE_LINK_RE.finditer(text):
        path, year, slug = m.group(1), int(m.group(2)), m.group(3)
        month_name = slug.split("-")[-2]
        month = _MONTHS.get(month_name)
        if month is None:
            continue
        if not path.endswith(".html"):
            path += ".html"
        pages[slug] = {
            "slug": f"{year}/{slug}", "year": year, "month": month, "url": WIKI_BASE_URL + path,
            "global": "global" in slug.lower(),
        }
    if not pages:
        raise ReweightParseError("reweight index has no month links")
    return sorted(pages.values(), key=lambda p: (p["year"], p["month"], p["slug"]))


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.h1: list[str] = []
        self.tables: list[list[list[str]]] = []
        self._in_h1 = False
        self._cell: Optional[list[str]] = None
        self._row: Optional[list[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag == "h1":
            self._in_h1 = True
            self.h1.append("")
        elif tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "h1":
            self._in_h1 = False
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self.tables:
            self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._in_h1 and self.h1:
            self.h1[-1] += data
        if self._cell is not None:
            self._cell.append(data)


def parse_page(text: str) -> dict:
    """月ごとのページを読む。

    戻り値 {"title", "date": date, "global": bool, "rows": [{"name", "difficulty", "mapper", "old", "new"}]}。
    old / new は読めなかった行では None。見出しの日付・表の見出しが想定と違えば ReweightParseError。
    """
    parser = _PageParser()
    parser.feed(text)
    title = next((h.strip() for h in parser.h1 if "reweight" in h.lower()), "")
    if not title:
        raise ReweightParseError("reweight page has no heading")
    m = _HEADING_DATE_RE.search(title)
    month = _MONTHS.get(m.group(2).capitalize()) if m else None
    if m is None or month is None:
        raise ReweightParseError(f"reweight heading has no date: {title!r}")
    page_date = date(int(m.group(3)), month, int(m.group(1)))
    is_global = "global" in title.lower()
    table = next(
        (t for t in parser.tables if t and [c.strip().lower() for c in t[0]] == _EXPECTED_HEADER),
        None,
    )
    if table is None:
        if is_global:
            return {"title": title, "date": page_date, "global": True, "rows": []}
        raise ReweightParseError("reweight page has no star change table")
    rows = []
    for cells in table[1:]:
        if len(cells) != 4:
            raise ReweightParseError(f"reweight row has {len(cells)} cells")
        sm = _STAR_CHANGE_RE.search(cells[3])
        rows.append({
            "name": cells[0], "difficulty": cells[1], "mapper": cells[2],
            "old": float(sm.group(1)) if sm else None,
            "new": float(sm.group(2)) if sm else None,
        })
    return {"title": title, "date": page_date, "global": is_global, "rows": rows}


# ---------------------------------------------------------------- 突き合わせ

def _norm(text: object) -> str:
    return " ".join(unicodedata.normalize("NFC", _html.unescape(str(text or ""))).casefold().split())


def _mapper_set(text: object) -> frozenset[str]:
    return frozenset(p for p in (_norm(x) for x in re.split(r"[&,]", str(text or ""))) if p)


def _name_matches(wiki_name: str, lb: dict) -> bool:
    name = _norm(wiki_name)
    song = _norm(lb.get("songName"))
    sub = _norm(lb.get("songSubName"))
    return bool(name) and (name == song or (bool(sub) and name == f"{song} {sub}"))


def _mapper_matches(wiki_mapper: str, lb: dict) -> bool:
    wiki = _norm(wiki_mapper)
    cached = _norm(lb.get("levelAuthorName"))
    return bool(wiki) and (wiki == cached or _mapper_set(wiki_mapper) == _mapper_set(lb.get("levelAuthorName")))


def find_candidates(row: dict, leaderboards: dict) -> list[str]:
    """曲名・難易度・譜面作者が合うキャッシュの leaderboard id を返す。"""
    diff = _DIFFICULTY_NUMBERS.get(_norm(row.get("difficulty")).replace(" ", ""))
    if diff is None:
        return []
    found = []
    for lb_id, lb in leaderboards.items():
        if not isinstance(lb, dict):
            continue
        d = lb.get("difficulty") if isinstance(lb.get("difficulty"), dict) else {}
        try:
            lb_diff = int(d.get("difficulty"))
        except (TypeError, ValueError):
            continue
        if lb_diff == diff and _name_matches(row["name"], lb) and _mapper_matches(row["mapper"], lb):
            found.append(str(lb_id))
    return found


def _same_star(a, b) -> bool:
    try:
        return float(a) > 0 and float(b) > 0 and not ranked_sync.stars_differ(a, b)
    except (TypeError, ValueError):
        return False


def classify_row(row: dict, leaderboards: dict) -> tuple[str, Optional[str]]:
    """1 行の判定。("reflected" | "refetch" | "flag", leaderboard id or None)。"""
    if row.get("old") is None or row.get("new") is None:
        return "flag", None
    ids = find_candidates(row, leaderboards)
    if len(ids) != 1:
        return "flag", None
    stars = leaderboards[ids[0]].get("stars")
    if _same_star(stars, row["new"]):
        return "reflected", ids[0]
    if _same_star(stars, row["old"]):
        return "refetch", ids[0]
    return "flag", ids[0]


# ---------------------------------------------------------------- 記録

def _read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _parse_dt(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _update_state(path: Path, fn: Callable[[dict], None]) -> None:
    """記録を読み直してから書き換える（ranked_sync と同じロックで 1 回にまとめる）。"""
    with ranked_sync._LOCK:  # noqa: SLF001
        data = _read_json(path)
        fn(data)
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _mark_processed(path: Path, slug: str, now: datetime, result: dict) -> None:
    def _fn(data: dict) -> None:
        processed = data.get("processed") if isinstance(data.get("processed"), dict) else {}
        processed[slug] = {"at": _iso(now), **result}
        data["processed"] = processed
    _update_state(path, _fn)


# ---------------------------------------------------------------- 本体

def _log(message: str) -> None:
    print(f"SS reweight: {message}")


def _fetch_text(session, url: str) -> str:
    resp = session.get(url, headers={"Accept": "text/html"}, timeout=15)
    if getattr(resp, "status_code", None) != 200:
        raise RuntimeError(f"HTTP {getattr(resp, 'status_code', None)} {url}")
    content = getattr(resp, "content", None)
    if isinstance(content, bytes):
        # charset の無い応答を requests が ISO-8859-1 として読むと「→」「⭐」や曲名が化けるので、UTF-8 で読む
        return content.decode("utf-8")
    return resp.text


def _fetch_leaderboard_info(session, lb: dict) -> dict:
    """譜面 1 件の情報を SS API から取り直す（playlist_view._resolve_ss_leaderboard_id_by_hash と同じ API）。"""
    song_hash = str(lb.get("songHash") or "")
    d = lb.get("difficulty") if isinstance(lb.get("difficulty"), dict) else {}
    if not song_hash or d.get("difficulty") is None:
        raise RuntimeError(f"no hash/difficulty for leaderboard {lb.get('id')}")
    resp = session.get(
        SCORESABER_LEADERBOARD_INFO_BY_HASH_URL.format(song_hash=song_hash),
        params={"difficulty": d.get("difficulty"), "gameMode": d.get("gameMode") or "SoloStandard"},
        timeout=15,
    )
    if getattr(resp, "status_code", None) != 200:
        raise RuntimeError(f"HTTP {getattr(resp, 'status_code', None)} leaderboard {lb.get('id')}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected leaderboard info for {lb.get('id')}")
    return data


def _positive(value) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _flag_drift(traversal_path: Path) -> bool:
    """★ずれの印を付け、書けたかを読み直して返す（書けなかったらページを処理済みにしない）。"""
    ranked_sync.flag_star_drift(traversal_path, "scoresaber")
    return bool(_read_json(traversal_path).get("scoresaber_stars_drift"))


class _AlreadyChecked(Exception):
    pass


def _claim_index_check(state_path: Path, now: datetime) -> bool:
    """一覧ページを読んでよいか（1 日 1 回まで）。読んでよければ、読む前に日時を記録する（失敗しても 1 日 1 回）。

    確認と記録を同じロックの中で行い、同時に 2 本動いても一覧を 2 回読まないようにする。
    """
    def _fn(data: dict) -> None:
        checked = _parse_dt(data.get("index_checked_at"))
        if checked is not None and timedelta(0) <= now - checked < timedelta(hours=INDEX_INTERVAL_HOURS):
            raise _AlreadyChecked
        data["index_checked_at"] = _iso(now)

    try:
        _update_state(state_path, _fn)
    except _AlreadyChecked:
        return False
    return True


def _month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


def apply_scoresaber_reweights(
    session,
    cache_dir: Path,
    now: Optional[datetime] = None,
) -> dict:
    """wiki のリウェイトのページで SS ランク譜面キャッシュの★を更新する。失敗しても例外は出さない（キャンセルだけは出す）。

    戻り値は結果のまとめ {"status": ..., "pages": [{slug, reflected, refetched, flagged, pending, ...}]}（ログ・テスト用）。
    """
    now = now or datetime.now(timezone.utc)
    cache_dir = Path(cache_dir)
    state_path = cache_dir / STATE_FILE_NAME
    traversal_path = cache_dir / "ranked_full_traversal.json"
    ranked_path = cache_dir / "scoresaber_ranked_maps.json"
    summary: dict = {"status": "", "pages": []}

    def _done(page: dict, result: dict) -> None:
        _mark_processed(state_path, page["slug"], now, {k: v for k, v in result.items() if k != "rows"})
        summary["pages"].append(result)

    def _flag_failed(page: dict) -> dict:
        _log(f"{page['slug']}: ★ずれの印を書けませんでした。処理済みにせず、次回に回します")
        summary["status"] = "page_failed"
        return summary

    try:
        complete = _parse_dt(_read_json(traversal_path).get("scoresaber_complete"))
        if complete is None:
            # 全件取得を終えていない＝この Snapshot で全件取得するので、リウェイトは見なくてよい
            summary["status"] = "no_full_traversal"
            return summary
        if not _claim_index_check(state_path, now):
            summary["status"] = "checked_recently"
            return summary

        try:
            index = parse_index(_fetch_text(session, REWEIGHTS_INDEX_URL))
        except Exception as exc:  # noqa: BLE001
            _rethrow_if_cancelled(exc)
            _log(f"一覧ページを読めませんでした（{exc}）。今回は何もしません")
            summary["status"] = "index_failed"
            return summary
        state = _read_json(state_path)
        processed = state.get("processed") if isinstance(state.get("processed"), dict) else {}
        # 全件取得の月の前月のページまで見る（前月分のページが翌月に遅れて出ることがあるため）。
        # それより前の月のページは読まない。読んだページは見出しの日付でさらに絞る。
        first_month = _month_index(complete.year, complete.month) - 1
        pages = [
            p for p in index
            if p["slug"] not in processed and _month_index(p["year"], p["month"]) >= first_month
        ]
        if not pages:
            summary["status"] = "no_new_pages"
            return summary

        raw = _read_json(ranked_path)
        leaderboards = raw.get("leaderboards") if isinstance(raw.get("leaderboards"), dict) else None
        if not leaderboards:
            _log("SS ランク譜面キャッシュがありません。今回は何もしません")
            summary["status"] = "no_cache"
            return summary

        # 見出しの日付はタイムゾーンが分からないので、全件取得の日の前日より前のページだけを「全件取得より前」とみなす
        cutoff = complete.date() - timedelta(days=1)
        status = "done"
        for page in pages:
            if page["global"]:
                # リンク名で全体リウェイトと分かる。本文は読まずに印だけ付ける（全件の取り直しに任せる）
                result = {"slug": page["slug"], "date": None, "reflected": 0, "refetched": 0, "flagged": 0,
                          "pending": 0, "rows": [], "skipped": "global_reweight"}
                if not _flag_drift(traversal_path):
                    return _flag_failed(page)
                _done(page, result)
                _log(f"{page['slug']}: 全体リウェイトのため★ずれの印を付けました")
                continue
            try:
                parsed = parse_page(_fetch_text(session, page["url"]))
            except Exception as exc:  # noqa: BLE001
                _rethrow_if_cancelled(exc)
                # 古い順に当てる（同じ譜面が 2 か月続けてリウェイトされたとき、旧→新の順に当てるため）。
                # 読めないページがあれば、それより新しいページも次回に回す
                _log(f"{page['slug']} を読めませんでした（{exc}）。処理済みにせず、残りは次回に回します")
                summary["status"] = "page_failed"
                return summary
            result = {"slug": page["slug"], "date": parsed["date"].isoformat(),
                      "reflected": 0, "refetched": 0, "flagged": 0, "pending": 0, "rows": []}
            if parsed["date"] < cutoff:
                # 全件取得より前のリウェイト。全件取得で既に入っている
                result["skipped"] = "before_full_traversal"
                _done(page, result)
                continue
            if parsed["global"]:
                result["skipped"] = "global_reweight"
                result["flagged"] = len(parsed["rows"])
                if not _flag_drift(traversal_path):
                    return _flag_failed(page)
                _done(page, result)
                continue

            updates: dict[str, dict] = {}
            flagged = False
            for row in parsed["rows"]:
                verdict, lb_id = classify_row(row, leaderboards)
                if verdict == "refetch" and lb_id is not None:
                    try:
                        info = _fetch_leaderboard_info(session, leaderboards[lb_id])
                    except Exception as exc:  # noqa: BLE001
                        _rethrow_if_cancelled(exc)
                        _log(f"{page['slug']} の譜面 {lb_id} を取り直せませんでした（{exc}）。処理済みにせず、残りは次回に回します")
                        summary["status"] = "page_failed"
                        return summary
                    if str(info.get("id")) != lb_id or not _positive(info.get("stars")):
                        # 別の譜面が返った・★が読めない
                        verdict = "flag"
                    elif _same_star(info.get("stars"), row["new"]):
                        updates[lb_id] = info
                    elif _same_star(info.get("stars"), row["old"]):
                        # SS にまだ反映されていない。書き換えず、このページは処理済みにしないで翌日以降に読み直す
                        # （処理済みにすると、窓の外の譜面なので後で反映されても気づけない）
                        verdict = "pending"
                    else:
                        # 「新」とも「旧」とも違う★が返った。API の値を入れ、全件の取り直しにも任せる
                        updates[lb_id] = info
                        verdict = "flag"
                if verdict == "flag":
                    flagged = True
                key = {"reflected": "reflected", "refetch": "refetched", "flag": "flagged", "pending": "pending"}[verdict]
                result[key] += 1
                result["rows"].append({**row, "verdict": verdict, "leaderboard_id": lb_id})

            if updates:
                for lb_id, info in updates.items():
                    lb = leaderboards[lb_id]
                    lb["stars"] = info.get("stars")
                    if info.get("maxPP") is not None:
                        lb["maxPP"] = info.get("maxPP")
                # fetched_at などは変えない（差分取得の 60 日の窓を進めないため）
                atomic_write_text(ranked_path, json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            if flagged and not _flag_drift(traversal_path):
                summary["pages"].append(result)
                return _flag_failed(page)
            _log(
                f"{page['slug']}: 反映済み {result['reflected']} / 取り直し {result['refetched']} / "
                f"印 {result['flagged']} / SS 未反映 {result['pending']}"
            )
            if result["pending"]:
                # SS が未反映の行が残った。処理済みにしない（翌日以降に読み直す）。古い順に当てるため、より新しいページも次回に回す
                summary["pages"].append(result)
                status = "pending"
                break
            _done(page, result)
        summary["status"] = status
        return summary
    except Exception as exc:  # noqa: BLE001
        _rethrow_if_cancelled(exc)
        _log(f"処理に失敗しました（{exc}）。今回は何もしません")
        summary["status"] = "error"
        return summary


def _rethrow_if_cancelled(exc: BaseException) -> None:
    if isinstance(exc, RuntimeError) and str(exc) == "SNAPSHOT_CANCELLED":
        raise exc
