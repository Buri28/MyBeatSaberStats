from datetime import datetime

from mybeatsaberstats.playlist_view import (
    MapEntry,
    _BatchConfig,
    _apply_config_filter,
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


def test_apply_config_filter_keeps_only_highest_difficulty_per_song_and_mode() -> None:
    expert = _entry(1714521600)
    expert.song_hash = "ABC123"
    expert.song_name = "Song"
    expert.mode = "Standard"
    expert.difficulty = "Expert"
    expert.stars = 7.5

    expert_plus = _entry(1714608000)
    expert_plus.song_hash = "ABC123"
    expert_plus.song_name = "Song"
    expert_plus.mode = "Standard"
    expert_plus.difficulty = "ExpertPlus"
    expert_plus.stars = 9.2

    one_saber = _entry(1714694400)
    one_saber.song_hash = "ABC123"
    one_saber.song_name = "Song"
    one_saber.mode = "OneSaber"
    one_saber.difficulty = "Expert"
    one_saber.stars = 8.0

    cfg = _BatchConfig(
        label="test",
        filename_base="",
        source="bs",
        highest_diff_only=True,
        sort_mode="date_desc",
    )

    result = _apply_config_filter([expert, expert_plus, one_saber], cfg)

    assert result == [one_saber, expert_plus]


def test_apply_config_filter_sorts_by_published_date_after_highest_difficulty_filter() -> None:
    older_high = _entry(1714521600)
    older_high.song_hash = "OLD"
    older_high.song_name = "Older"
    older_high.difficulty = "ExpertPlus"
    older_high.stars = 8.5

    newer_low = _entry(1714780800)
    newer_low.song_hash = "NEW"
    newer_low.song_name = "Newer"
    newer_low.difficulty = "Hard"
    newer_low.stars = 5.0

    older_low_duplicate = _entry(1714435200)
    older_low_duplicate.song_hash = "OLD"
    older_low_duplicate.song_name = "Older"
    older_low_duplicate.difficulty = "Expert"
    older_low_duplicate.stars = 7.0

    cfg = _BatchConfig(
        label="test",
        filename_base="",
        source="bs",
        highest_diff_only=True,
        sort_mode="date_desc",
    )

    result = _apply_config_filter([older_high, newer_low, older_low_duplicate], cfg)

    assert result == [newer_low, older_high]


def test_batch_config_display_text_shows_highest_difficulty_filter() -> None:
    cfg = _BatchConfig(
        label="test",
        filename_base="",
        source="bs",
        highest_diff_only=True,
        sort_mode="date_desc",
    )

    assert "TopDiff" in cfg.display_text()