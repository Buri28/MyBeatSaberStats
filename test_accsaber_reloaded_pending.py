from mybeatsaberstats.accsaber_reloaded import (
    build_unplayed_bplist,
    is_active_difficulty,
    is_pending_difficulty,
)


def test_is_pending_difficulty_detects_pending_states() -> None:
    assert is_pending_difficulty({"active": True, "criteriaStatus": "PENDING", "status": "QUEUE"}) is True
    assert is_pending_difficulty({"active": True, "criteriaStatus": "PENDING", "status": "RANKED"}) is False
    assert is_pending_difficulty({"active": True, "criteriaStatus": "APPROVED", "status": "RANKED"}) is False


def test_is_active_difficulty_treats_ranked_without_active_as_active() -> None:
    assert is_active_difficulty({"status": "RANKED"}) is True
    assert is_active_difficulty({"status": "QUEUE"}) is False
    assert is_active_difficulty({"active": False, "status": "RANKED"}) is False


def test_build_unplayed_bplist_keeps_pending_song_name_unchanged() -> None:
    all_maps = [
        {
            "songHash": "abc123",
            "songName": "Capsize",
            "difficulties": [
                {
                    "active": True,
                    "categoryId": "b0000000-0000-0000-0000-000000000002",
                    "characteristic": "Standard",
                    "difficulty": "EXPERT",
                    "criteriaStatus": "PENDING",
                    "status": "QUEUE",
                    "id": "diff-pending",
                }
            ],
        }
    ]

    bplist = build_unplayed_bplist(all_maps, set(), "standard")

    assert bplist["songs"] == [
        {
            "hash": "abc123",
            "songName": "Capsize",
            "difficulties": [{"characteristic": "Standard", "name": "Expert"}],
        }
    ]


def test_build_unplayed_bplist_keeps_ranked_song_without_active_field() -> None:
    all_maps = [
        {
            "songHash": "def456",
            "songName": "Sharks",
            "difficulties": [
                {
                    "categoryId": "b0000000-0000-0000-0000-000000000002",
                    "characteristic": "Standard",
                    "difficulty": "NORMAL",
                    "status": "RANKED",
                    "id": "diff-ranked",
                }
            ],
        }
    ]

    bplist = build_unplayed_bplist(all_maps, set(), "standard")

    assert bplist["songs"] == [
        {
            "hash": "def456",
            "songName": "Sharks",
            "difficulties": [{"characteristic": "Standard", "name": "Normal"}],
        }
    ]
