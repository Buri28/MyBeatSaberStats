"""外部 API を叩く際の共通 HTTP クライアント。

このアプリは ScoreSaber / AccSaber Reloaded / BeatLeader / BeatSaver という
個人・少人数で運営されているサービスの公開 API に依存している。
各呼び出し元がそれぞれ ``requests.Session()`` を作って無制限にリクエストを
投げると、サーバ側に不必要な負荷をかけ、レート制限や BAN の原因になる。

このモジュールが提供するもの:

* アプリ名・バージョン・連絡先を含む User-Agent（運営側が識別・連絡できるように）
* ホストごとの最小リクエスト間隔（トークンバケットではなく単純な間隔制御）
* ホストごとの同時接続数上限
* 429 / 503 を受けた際の ``Retry-After`` 準拠の待機と、そのホスト全体のクールダウン

呼び出し元は ``requests.Session()`` の代わりに :func:`make_session` を使うだけでよい。
"""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import requests

from .snapshot import BASE_DIR


APP_NAME = "MyBeatSaberStats"
PROJECT_URL = "https://github.com/Buri28/MyBeatSaberStats"


def _read_app_version() -> str:
    """version.json からバージョンを読む。取得できなければ "unknown"。"""
    candidates = [
        BASE_DIR / "version.json",
        Path(__file__).resolve().parents[2] / "version.json",
    ]
    for path in candidates:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        version = str(raw.get("version") or "").strip()
        if version:
            return version
    return "unknown"


APP_VERSION = _read_app_version()

#: 全リクエストに付与する User-Agent。
#: 運営側が「どのアプリが叩いているのか」「どこに連絡すればよいか」を判別できるようにする。
USER_AGENT = f"{APP_NAME}/{APP_VERSION} (+{PROJECT_URL})"


class _HostPolicy:
    """1 ホストに対するリクエスト間隔・同時接続数・クールダウンを管理する。"""

    def __init__(self, min_interval: float, max_concurrency: int, max_retries: int = 3) -> None:
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._semaphore = threading.Semaphore(max_concurrency)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire_slot(self) -> None:
        self._semaphore.acquire()

    def release_slot(self) -> None:
        self._semaphore.release()

    def wait_turn(self) -> None:
        """最小間隔（およびクールダウン）を満たすまで待つ。"""
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._next_allowed:
                    # 自分の分の枠を確保してから抜ける
                    self._next_allowed = now + self.min_interval
                    return
                sleep_for = self._next_allowed - now
            time.sleep(sleep_for)

    def penalize(self, seconds: float) -> None:
        """429 などを受けた際、このホスト宛の全リクエストを一定時間止める。"""
        with self._lock:
            target = time.monotonic() + seconds
            if target > self._next_allowed:
                self._next_allowed = target


# ホストごとのポリシー。
#
# min_interval はそのホストへ連続してリクエストを出す際の最小間隔（秒）。
# max_concurrency は同時に飛ばしてよいリクエスト数。
_POLICIES: Dict[str, _HostPolicy] = {
    # ScoreSaber は公式に 400 リクエスト/分の制限があり、超過すると一時的に
    # ブロックされる。余裕を持って ~150 req/min 相当に抑え、直列で叩く。
    "scoresaber.com": _HostPolicy(min_interval=0.40, max_concurrency=1, max_retries=4),
    # AccSaber Reloaded は個人運営で規模も小さいため、最も保守的に扱う。
    "api.accsaberreloaded.com": _HostPolicy(min_interval=0.50, max_concurrency=1, max_retries=4),
    "accsaber.com": _HostPolicy(min_interval=0.50, max_concurrency=1, max_retries=4),
    # BeatLeader は Retry-After を返してくれるので、それに従いつつ控えめに。
    "api.beatleader.xyz": _HostPolicy(min_interval=0.12, max_concurrency=4, max_retries=3),
    "beatleader.com": _HostPolicy(min_interval=0.12, max_concurrency=4, max_retries=3),
    # BeatSaver はまとめ取得 API があるため、そもそもリクエスト数自体が少ない。
    "beatsaver.com": _HostPolicy(min_interval=0.20, max_concurrency=2, max_retries=3),
    "api.beatsaver.com": _HostPolicy(min_interval=0.20, max_concurrency=2, max_retries=3),
    "cdn.beatsaver.com": _HostPolicy(min_interval=0.05, max_concurrency=4, max_retries=2),
}

#: 明示的なポリシーが無いホスト向けの既定値。
_DEFAULT_POLICY = _HostPolicy(min_interval=0.20, max_concurrency=4, max_retries=3)

_UNKNOWN_HOST_POLICIES: Dict[str, _HostPolicy] = {}
_UNKNOWN_HOST_LOCK = threading.Lock()


def _policy_for(url: str) -> _HostPolicy:
    host = (urlparse(url).hostname or "").lower()
    policy = _POLICIES.get(host)
    if policy is not None:
        return policy
    if not host:
        return _DEFAULT_POLICY
    # 未知ホストもホスト単位で直列化されるよう、個別のポリシーを作って使い回す。
    with _UNKNOWN_HOST_LOCK:
        policy = _UNKNOWN_HOST_POLICIES.get(host)
        if policy is None:
            policy = _HostPolicy(
                min_interval=_DEFAULT_POLICY.min_interval,
                max_concurrency=4,
                max_retries=_DEFAULT_POLICY.max_retries,
            )
            _UNKNOWN_HOST_POLICIES[host] = policy
        return policy


def _retry_after_seconds(resp: requests.Response, attempt: int) -> float:
    """Retry-After ヘッダを解釈する。無い場合は指数バックオフ。"""
    header = resp.headers.get("Retry-After")
    if header:
        try:
            value = float(header)
            # 極端に長い指定は上限を設ける（UI がフリーズしないように）
            return max(0.0, min(value, 60.0))
        except (TypeError, ValueError):
            pass
    return min(30.0, 2.0 ** attempt)


#: レート制限を検知した回数（デバッグ・ログ用）。
throttle_events: Dict[str, int] = {}
_THROTTLE_LOCK = threading.Lock()


def _record_throttle(url: str) -> None:
    host = (urlparse(url).hostname or "unknown").lower()
    with _THROTTLE_LOCK:
        throttle_events[host] = throttle_events.get(host, 0) + 1


# ---------------------------------------------------------------------------
# リクエスト数の計測とログ出力
# ---------------------------------------------------------------------------

REQUEST_LOG_PATH = BASE_DIR / "logs" / "api_requests.log"


@dataclass
class HostStats:
    """1 ホストへのリクエスト実績。"""

    requests: int = 0          # 実際に送信した HTTP リクエスト数（リトライも 1 回と数える）
    rate_limited: int = 0      # 429 / 503 を受けた回数
    errors: int = 0            # 4xx / 5xx を受けた回数（404 を除く）
    not_found: int = 0         # 404 の回数
    wait_seconds: float = 0.0  # レート制御で待った合計秒数

    def copy(self) -> "HostStats":
        return HostStats(
            requests=self.requests,
            rate_limited=self.rate_limited,
            errors=self.errors,
            not_found=self.not_found,
            wait_seconds=self.wait_seconds,
        )

    def diff(self, base: "HostStats") -> "HostStats":
        return HostStats(
            requests=self.requests - base.requests,
            rate_limited=self.rate_limited - base.rate_limited,
            errors=self.errors - base.errors,
            not_found=self.not_found - base.not_found,
            wait_seconds=self.wait_seconds - base.wait_seconds,
        )


#: (ホスト, "METHOD /path/template") をキーにした実績。
_STATS: Dict[Tuple[str, str], HostStats] = {}
_STATS_LOCK = threading.Lock()

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX_RE = re.compile(r"^[0-9a-f]{32,}$", re.I)


def _host_of(url: str) -> str:
    return (urlparse(str(url)).hostname or "unknown").lower()


def _normalize_segment(segment: str) -> str:
    """パスの 1 セグメントを、可変部分ならプレースホルダへ置き換える。"""
    if not segment:
        return segment
    if segment.isdigit():
        # プレイヤー ID・ページ番号など
        return "{id}"
    if _UUID_RE.match(segment):
        # AccSaber のカテゴリ UUID・難易度 UUID など
        return "{uuid}"
    if "," in segment:
        # 一括指定（BeatSaver の /maps/hash/{h1},{h2},... や /maps/ids/{id1},{id2},...）。
        # 個々の値をそのまま出すとログが極端に長くなるため必ず潰す。
        parts = [p for p in segment.split(",") if p]
        if parts and all(_HEX_RE.match(p) for p in parts):
            return "{hashes}"
        return "{ids}"
    if _HEX_RE.match(segment):
        # 譜面ハッシュ（40 桁 hex など）
        return "{hash}"
    return segment


def _endpoint_of(method: str, url: str) -> str:
    """URL を "GET /users/{id}/milestones" のような識別子へ正規化する。

    プレイヤー ID・UUID・譜面ハッシュといった可変部分をプレースホルダに
    置き換えることで、同じエンドポイントへの呼び出しをまとめて数えられる。
    クエリ文字列（page など）は落とす。
    """
    path = urlparse(str(url)).path or "/"

    segments: list[str] = []
    for raw in path.split("/"):
        # 直前が "by-code" / "hash" なら、続く値は必ず可変部分（数字でも hex でもない
        # BeatSaver コードなど）なのでプレースホルダに潰す
        if segments and segments[-1] in ("by-code", "code", "key"):
            segments.append("{code}")
            continue
        if segments and segments[-1] == "ids":
            segments.append("{ids}")
            continue
        segments.append(_normalize_segment(raw))

    normalized = "/".join(segments) or "/"
    return f"{method.upper()} {normalized}"


def _record_request(host: str, endpoint: str, status_code: Optional[int], waited: float) -> None:
    with _STATS_LOCK:
        stats = _STATS.setdefault((host, endpoint), HostStats())
        stats.requests += 1
        stats.wait_seconds += waited
        if status_code is None:
            stats.errors += 1
        elif status_code in (429, 503):
            stats.rate_limited += 1
        elif status_code == 404:
            stats.not_found += 1
        elif status_code >= 400:
            stats.errors += 1


def get_request_stats() -> Dict[Tuple[str, str], HostStats]:
    """(ホスト, エンドポイント) 別のリクエスト実績のスナップショットを返す。"""
    with _STATS_LOCK:
        return {key: stats.copy() for key, stats in _STATS.items()}


def reset_request_stats() -> None:
    """カウンタを 0 に戻す。"""
    with _STATS_LOCK:
        _STATS.clear()


def _notes_for(s: HostStats) -> str:
    notes = []
    if s.rate_limited:
        notes.append(f"429/503 {s.rate_limited} 回")
    if s.errors:
        notes.append(f"エラー {s.errors} 回")
    if s.not_found:
        notes.append(f"404 {s.not_found} 回")
    return " ※" + ", ".join(notes) if notes else ""


def format_request_summary(
    label: str,
    stats: Dict[Tuple[str, str], HostStats],
    elapsed: Optional[float] = None,
) -> str:
    """ホスト別・エンドポイント別のリクエスト数を 1 つの文字列に整形する。"""
    active = {key: s for key, s in stats.items() if s.requests > 0}
    total = sum(s.requests for s in active.values())

    header = f"[{datetime.now().isoformat(timespec='seconds')}] {label}: 合計 {total} リクエスト"
    if elapsed is not None:
        header += f" / {elapsed:.1f}秒"
    if not active:
        return header + "（リクエストなし）"

    # ホスト単位に畳んだ合計を作る
    per_host: Dict[str, HostStats] = {}
    per_host_endpoints: Dict[str, Dict[str, HostStats]] = {}
    for (host, endpoint), s in active.items():
        agg = per_host.setdefault(host, HostStats())
        agg.requests += s.requests
        agg.rate_limited += s.rate_limited
        agg.errors += s.errors
        agg.not_found += s.not_found
        agg.wait_seconds += s.wait_seconds
        per_host_endpoints.setdefault(host, {})[endpoint] = s

    lines = [header]
    # 多く叩いた API から順に並べ、その下にエンドポイント内訳を出す
    for host, s in sorted(per_host.items(), key=lambda kv: kv[1].requests, reverse=True):
        detail = f"  {host}: {s.requests} 回"
        if s.wait_seconds >= 0.05:
            detail += f" (レート制御待ち {s.wait_seconds:.1f}秒)"
        lines.append(detail + _notes_for(s))

        endpoints = per_host_endpoints.get(host, {})
        for endpoint, es in sorted(endpoints.items(), key=lambda kv: kv[1].requests, reverse=True):
            lines.append(f"    {endpoint}: {es.requests} 回" + _notes_for(es))
    return "\n".join(lines)


def log_request_summary(
    label: str,
    stats: Dict[Tuple[str, str], HostStats],
    elapsed: Optional[float] = None,
) -> None:
    """サマリをコンソールと logs/api_requests.log の両方へ出力する。"""
    summary = format_request_summary(label, stats, elapsed)
    print(summary)
    try:
        REQUEST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with REQUEST_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(summary + "\n")
    except Exception:
        pass


@contextmanager
def request_scope(label: str):
    """一連の API 呼び出しを囲み、抜けるときにホスト別リクエスト数をログへ出す。

    使い方::

        with request_scope("スナップショット取得"):
            ...  # この中の全リクエストが集計される

    ワーカースレッドから出たリクエストも含めて数えられるよう、グローバルな
    カウンタの差分で集計する。例外で抜けた場合もサマリは出力する。
    """
    before = get_request_stats()
    started = time.monotonic()
    try:
        yield
    finally:
        after = get_request_stats()
        delta = {
            key: stats.diff(before.get(key, HostStats()))
            for key, stats in after.items()
        }
        log_request_summary(label, delta, elapsed=time.monotonic() - started)


class PoliteSession(requests.Session):
    """ホスト単位でレート制限と 429 リトライを行う ``requests.Session``。

    既存コードは ``session.get(...)`` をそのまま呼べばよく、
    間隔制御・同時実行数制限・429 バックオフは透過的に適用される。
    """

    #: timeout 未指定の呼び出しに適用する既定値 (connect, read)
    default_timeout = (5, 15)

    def request(self, method, url, **kwargs):  # type: ignore[override]
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.default_timeout

        policy = _policy_for(str(url))
        host = _host_of(url)
        endpoint = _endpoint_of(method, url)
        policy.acquire_slot()
        try:
            last_resp: Optional[requests.Response] = None
            for attempt in range(1, policy.max_retries + 1):
                wait_started = time.monotonic()
                policy.wait_turn()
                waited = time.monotonic() - wait_started

                try:
                    resp = super().request(method, url, **kwargs)
                except Exception:
                    # 接続エラーなども「投げたリクエスト」として記録する
                    _record_request(host, endpoint, None, waited)
                    raise

                _record_request(host, endpoint, resp.status_code, waited)

                if resp.status_code not in (429, 503):
                    return resp

                _record_throttle(str(url))
                wait = _retry_after_seconds(resp, attempt)
                # このホスト宛の他スレッドのリクエストもまとめて止める
                policy.penalize(wait)
                last_resp = resp
                if attempt >= policy.max_retries:
                    break
                resp.close()
            # リトライを使い切った場合は最後のレスポンスを返し、
            # 判断（エラー扱いにするか）は呼び出し元に委ねる。
            return last_resp if last_resp is not None else resp
        finally:
            policy.release_slot()


def make_session(extra_headers: Optional[Dict[str, str]] = None) -> PoliteSession:
    """User-Agent とレート制限を備えたセッションを作る。

    アプリ内で ``requests.Session()`` を直接使わず、必ずこれを経由すること。
    """
    session = PoliteSession()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
    )
    if extra_headers:
        session.headers.update(extra_headers)
    return session


_SHARED_SESSION: Optional[PoliteSession] = None
_SHARED_LOCK = threading.Lock()


def get_shared_session() -> PoliteSession:
    """使い捨てセッションを作りたくない場面向けの共有セッション。

    コネクションを再利用できるため、TCP/TLS ハンドシェイクの回数も減らせる。
    """
    global _SHARED_SESSION
    if _SHARED_SESSION is None:
        with _SHARED_LOCK:
            if _SHARED_SESSION is None:
                _SHARED_SESSION = make_session()
    return _SHARED_SESSION
