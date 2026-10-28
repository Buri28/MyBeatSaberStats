"""スコアの修飾子（modifiers）の読み方と、クリアかどうかの判定。

★統計の Clears / NF / SS / NA と、プレイリストの ✔ / NF / ✖ は、ここの判定にそろえる。

修飾子ごとの扱い（1 つずつ決めている）:
- NF (No Fail)        … クリアにしない。NF 列に数える（最優先）。
- SS (Slower Song)    … クリアにしない。SS 列に数える。
- NA (No Arrows)      … クリアにしない。NA 列に数える。
- それ以外（FS / SF / GN / DA / BE / NB / NO / PM / SC / IF / OD / OP / ZM など）… クリアとして数える。
  曲を最後まで通常の速さ・通常のノーツで遊んでいるため。
両方付いていたら NF > SS > NA の順で 1 つの列に入れる（ScoreSaber と BeatLeader で同じ）。
"""
from __future__ import annotations

import re
from typing import Literal

PlayKind = Literal["clear", "nf", "ss", "na"]

_SEPARATORS = re.compile(r"[,\s;|]+")


def parse_modifiers(raw: object) -> set[str]:
    """修飾子の文字列を 1 つずつの集合にする。

    ScoreSaber / BeatLeader とも普段は "NF,FS" のようにカンマ区切り。
    区切りが無く 3 文字以上なら（古い形式の "NFFS" など）2 文字ずつに分ける。
    """
    if raw is None:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(m).strip().upper() for m in raw if str(m).strip()}
    text = str(raw).strip().upper()
    if not text:
        return set()
    if _SEPARATORS.search(text):
        return {part for part in _SEPARATORS.split(text) if part}
    if len(text) > 2 and len(text) % 2 == 0:
        return {text[i:i + 2] for i in range(0, len(text), 2)}
    return {text}


def classify_play(modifiers: object) -> PlayKind:
    """修飾子から、クリア / NF / SS / NA のどれに数えるかを返す。"""
    mods = modifiers if isinstance(modifiers, set) else parse_modifiers(modifiers)
    if "NF" in mods:
        return "nf"
    if "SS" in mods:
        return "ss"
    if "NA" in mods:
        return "na"
    return "clear"


def format_modifiers(modifiers: object) -> str:
    """表示用に "NF,FS" の形へ整える（元の並び順を保つ）。"""
    if modifiers is None:
        return ""
    text = str(modifiers).strip().upper() if not isinstance(modifiers, (list, tuple, set)) else ",".join(
        str(m).strip().upper() for m in modifiers
    )
    if not text:
        return ""
    if _SEPARATORS.search(text):
        return ",".join(part for part in _SEPARATORS.split(text) if part)
    if len(text) > 2 and len(text) % 2 == 0:
        return ",".join(text[i:i + 2] for i in range(0, len(text), 2))
    return text
