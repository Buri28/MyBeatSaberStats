from datetime import datetime

from mybeatsaberstats.playlist_view import (
    MapEntry,
    _group_entries_by_month,
    _group_entries_by_week,
    _split_end_of_month,
)


def _entry(ts: int) -> MapEntry:
    return MapEntry(
        song_name="song",
        song_author="author",
        mapper="mapper",
        song_hash="hash",
        difficulty="Expert",
        mode="Standard",
        stars=10.0,
        max_pp=0.0,
        player_pp=0.0,
        cleared=False,
        nf_clear=False,
        player_acc=0.0,
        player_rank=0,
        leaderboard_id="lb",
        source="scoresaber",
        source_date_ts=ts,
    )


def test_group_entries_by_week_uses_monday_start() -> None:
    monday = datetime(2026, 4, 20, 8, 0).timestamp()
    sunday = datetime(2026, 4, 26, 23, 59).timestamp()
    next_monday = datetime(2026, 4, 27, 0, 0).timestamp()

    groups = _group_entries_by_week([
        _entry(int(monday)),
        _entry(int(sunday)),
        _entry(int(next_monday)),
    ])

    starts = sorted(key for key in groups.keys() if key is not None)
    assert [start.strftime("%Y-%m-%d") for start in starts] == ["2026-04-20", "2026-04-27"]
    assert len(groups[starts[0]]) == 2
    assert len(groups[starts[1]]) == 1


def test_group_entries_by_month_uses_calendar_month_range() -> None:
    april = datetime(2026, 4, 30, 23, 59).timestamp()
    may = datetime(2026, 5, 1, 0, 0).timestamp()

    groups = _group_entries_by_month([
        _entry(int(april)),
        _entry(int(may)),
    ])

    starts = sorted(key for key in groups.keys() if key is not None)
    assert [start.strftime("%Y-%m-%d") for start in starts] == ["2026-04-01", "2026-05-01"]
    assert _split_end_of_month(starts[0]).strftime("%Y-%m-%d") == "2026-04-30"
    assert _split_end_of_month(starts[1]).strftime("%Y-%m-%d") == "2026-05-31"