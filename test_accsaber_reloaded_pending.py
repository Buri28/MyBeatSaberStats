import json
from typing import cast

import requests

from mybeatsaberstats.accsaber_reloaded import (
    build_unplayed_bplist,
    fetch_and_save_all_maps_cache,
    get_reloaded_map_counts_from_cache,
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


def test_get_reloaded_map_counts_prefers_dedicated_count_cache(monkeypatch, tmp_path) -> None:
    import mybeatsaberstats.accsaber_reloaded as mod

    monkeypatch.setattr(mod, "_MAP_COUNTS_CACHE_FILE", tmp_path / "counts.json")
    monkeypatch.setattr(mod, "_ALL_MAPS_CACHE_FILE", tmp_path / "maps.json")

    mod._MAP_COUNTS_CACHE_FILE.write_text(
        json.dumps(
            {
                "true": {"count": 120},
                "standard": {"count": 226},
                "tech": {"count": 180},
            }
        ),
        encoding="utf-8",
    )
    mod._ALL_MAPS_CACHE_FILE.write_text(
        json.dumps(
            {
                "maps": [
                    {
                        "difficulties": [
                            {
                                "categoryId": mod.CATEGORY_IDS["true"],
                                "status": "RANKED",
                                "id": "stale-1",
                            }
                        ]
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert get_reloaded_map_counts_from_cache() == {
        "true": 120,
        "standard": 226,
        "tech": 180,
        "overall": 526,
    }


def test_fetch_and_save_all_maps_cache_syncs_count_cache(monkeypatch, tmp_path) -> None:
    import mybeatsaberstats.accsaber_reloaded as mod

    monkeypatch.setattr(mod, "_MAP_COUNTS_CACHE_FILE", tmp_path / "counts.json")
    monkeypatch.setattr(mod, "_ALL_MAPS_CACHE_FILE", tmp_path / "maps.json")

    fresh_maps = [
        {
            "difficulties": [
                {
                    "categoryId": mod.CATEGORY_IDS["true"],
                    "status": "RANKED",
                    "id": "true-1",
                },
                {
                    "categoryId": mod.CATEGORY_IDS["standard"],
                    "status": "RANKED",
                    "id": "std-1",
                },
                {
                    "categoryId": mod.CATEGORY_IDS["tech"],
                    "status": "RANKED",
                    "id": "tech-1",
                },
                {
                    "categoryId": mod.CATEGORY_IDS["tech"],
                    "status": "QUEUE",
                    "id": "tech-pending",
                },
            ]
        }
    ]

    monkeypatch.setattr(mod, "fetch_all_maps_full", lambda session=None, on_progress=None: fresh_maps)

    fetch_and_save_all_maps_cache()

    assert get_reloaded_map_counts_from_cache() == {
        "true": 1,
        "standard": 1,
        "tech": 1,
        "overall": 3,
    }


def test_fetch_reloaded_map_counts_uses_batch_fallback_when_maps_api_fails(monkeypatch, tmp_path) -> None:
    import mybeatsaberstats.accsaber_reloaded as mod

    class _FakeResponse:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self) -> dict:
            return self._payload

    class _FakeSession:
        def get(self, url, params=None, timeout=None):  # noqa: ANN001, ANN202
            if url.endswith("/maps"):
                return _FakeResponse(500, {})
            if url.endswith("/batches"):
                return _FakeResponse(
                    200,
                    {
                        "content": [
                            {
                                "releasedAt": "2026-06-29T20:34:31.307996Z",
                                "difficulties": [
                                    {"categoryId": mod.CATEGORY_IDS["true"], "status": "RANKED", "id": "t1"},
                                    {"categoryId": mod.CATEGORY_IDS["true"], "status": "RANKED", "id": "t2"},
                                    {"categoryId": mod.CATEGORY_IDS["true"], "status": "RANKED", "id": "t3"},
                                    {"categoryId": mod.CATEGORY_IDS["true"], "status": "RANKED", "id": "t4"},
                                    {"categoryId": mod.CATEGORY_IDS["standard"], "status": "RANKED", "id": "s1"},
                                    {"categoryId": mod.CATEGORY_IDS["standard"], "status": "RANKED", "id": "s2"},
                                    {"categoryId": mod.CATEGORY_IDS["standard"], "status": "RANKED", "id": "s3"},
                                    {"categoryId": mod.CATEGORY_IDS["standard"], "status": "RANKED", "id": "s4"},
                                    {"categoryId": mod.CATEGORY_IDS["standard"], "status": "RANKED", "id": "s5"},
                                    {"categoryId": mod.CATEGORY_IDS["standard"], "status": "RANKED", "id": "s6"},
                                    {"categoryId": mod.CATEGORY_IDS["standard"], "status": "RANKED", "id": "s7"},
                                    {"categoryId": mod.CATEGORY_IDS["standard"], "status": "RANKED", "id": "s8"},
                                    {"categoryId": mod.CATEGORY_IDS["tech"], "status": "RANKED", "id": "x1"},
                                    {"categoryId": mod.CATEGORY_IDS["tech"], "status": "RANKED", "id": "x2"},
                                    {"categoryId": mod.CATEGORY_IDS["tech"], "status": "RANKED", "id": "x3"},
                                    {"categoryId": mod.CATEGORY_IDS["tech"], "status": "RANKED", "id": "x4"},
                                    {"categoryId": mod.CATEGORY_IDS["tech"], "status": "RANKED", "id": "x5"},
                                    {"categoryId": mod.CATEGORY_IDS["tech"], "status": "RANKED", "id": "x6"},
                                    {"categoryId": mod.CATEGORY_IDS["tech"], "status": "RANKED", "id": "x7"},
                                ],
                            },
                            {
                                "releasedAt": "2026-06-01T00:00:00Z",
                                "difficulties": [
                                    {"categoryId": mod.CATEGORY_IDS["standard"], "status": "RANKED", "id": "old"}
                                ],
                            },
                        ],
                        "last": True,
                    },
                )
            raise AssertionError(url)

    monkeypatch.setattr(mod, "_MAP_COUNTS_CACHE_FILE", tmp_path / "counts.json")
    monkeypatch.setattr(mod, "_ALL_MAPS_CACHE_FILE", tmp_path / "maps.json")
    mod._MAP_COUNTS_CACHE_FILE.write_text(
        json.dumps(
            {
                "fetched_at": "2026-06-20T16:33:52Z",
                "true": {"count": 119},
                "standard": {"count": 225},
                "tech": {"count": 179},
            }
        ),
        encoding="utf-8",
    )

    counts = mod.fetch_reloaded_map_counts(session=cast(requests.Session, _FakeSession()))

    assert counts == {
        "true": 123,
        "standard": 233,
        "tech": 186,
        "overall": 542,
    }
    assert get_reloaded_map_counts_from_cache() == counts


def test_fetch_all_maps_via_difficulties_backfills_pending_song_hash() -> None:
    """difficulties/all は ranked 分の hash しか返さないので by-code で補うこと。"""
    import mybeatsaberstats.accsaber_reloaded as mod

    class _FakeResponse:
        def __init__(self, payload) -> None:  # noqa: ANN001
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):  # noqa: ANN201
            return self._payload

    class _FakeSession:
        def get(self, url, params=None, timeout=None):  # noqa: ANN001, ANN202
            if url.endswith("/maps/difficulties/all"):
                # ranked の分だけ songHash を持つ
                return _FakeResponse([{"id": "diff-ranked", "songHash": "RANKEDHASH"}])
            if url.endswith("/maps/difficulties"):
                return _FakeResponse(
                    {
                        "content": [
                            {
                                "id": "diff-ranked",
                                "mapId": "map-ranked",
                                "beatsaverCode": "aaaaa",
                                "songName": "Ranked Song",
                                "status": "RANKED",
                            },
                            {
                                "id": "diff-pending",
                                "mapId": "map-pending",
                                "beatsaverCode": "bbbbb",
                                "songName": "Pending Song",
                                "status": "QUEUE",
                                "criteriaStatus": "PENDING",
                            },
                        ],
                        "last": True,
                        "totalPages": 1,
                    }
                )
            if url.endswith("/maps/by-code/bbbbb"):
                return _FakeResponse({"beatsaverCode": "bbbbb", "songHash": "PENDINGHASH"})
            raise AssertionError(url)

    maps = mod._fetch_all_maps_via_difficulties(cast(requests.Session, _FakeSession()))

    hashes = {str(song.get("beatsaverCode")): song.get("songHash") for song in maps}
    assert hashes == {"aaaaa": "RANKEDHASH", "bbbbb": "PENDINGHASH"}


def test_search_in_leaderboard_filters_country_by_query_param() -> None:
    """旧 /leaderboards/{uuid}/country/{cc} は 404 になったのでクエリ方式であること。"""
    import mybeatsaberstats.accsaber_reloaded as mod

    seen: list = []

    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "content": [
                    {
                        "userId": "76561198324870685",
                        "userName": "Buri",
                        "country": "JP",
                        "ap": 23877.551515,
                        "averageAcc": 0.980895,
                        "rankedPlays": 542,
                        "ranking": 483,
                        "countryRanking": 28,
                    }
                ],
                "last": True,
            }

    class _FakeSession:
        def get(self, url, params=None, timeout=None):  # noqa: ANN001, ANN202
            seen.append((url, params))
            return _FakeResponse()

    player = mod._search_in_leaderboard(
        mod.CATEGORY_IDS["overall"],
        "76561198324870685",
        "jp",
        cast(requests.Session, _FakeSession()),
    )

    url, params = seen[0]
    assert url == f"{mod.BASE_URL}/leaderboards/{mod.CATEGORY_IDS['overall']}"
    assert "/country/" not in url
    assert params["country"] == "JP"
    assert player is not None
    assert (player.ap, player.rank_global, player.rank_country) == (23877.551515, 483, 28)
    assert player.average_acc == 0.980895


def test_load_all_maps_with_recent_batch_fallback_merges_full_maps(monkeypatch, tmp_path) -> None:
    import mybeatsaberstats.accsaber_reloaded as mod

    class _FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return self._payload

    class _FakeSession:
        def get(self, url, params=None, timeout=None):  # noqa: ANN001, ANN202
            if url.endswith("/batches"):
                return _FakeResponse(
                    {
                        "content": [
                            {
                                "releasedAt": "2026-06-29T20:34:31.307996Z",
                                "difficulties": [
                                    {"beatsaverCode": "new01"},
                                ],
                            }
                        ],
                        "last": True,
                    }
                )
            if url.endswith("/maps/by-code/new01"):
                return _FakeResponse(
                    {
                        "beatsaverCode": "new01",
                        "id": "map-new-01",
                        "songHash": "ABCDEF0123456789",
                        "songName": "New Song",
                        "songAuthor": "Artist",
                        "songSubName": "",
                        "mapAuthor": "Mapper",
                        "difficulties": [
                            {
                                "id": "diff-new-01",
                                "categoryId": mod.CATEGORY_IDS["standard"],
                                "difficulty": "HARD",
                                "characteristic": "Standard",
                                "status": "RANKED",
                                "blLeaderboardId": "bl-new-01",
                                "ssLeaderboardId": "ss-new-01",
                            }
                        ],
                    }
                )
            raise AssertionError(url)

    monkeypatch.setattr(mod, "_ALL_MAPS_CACHE_FILE", tmp_path / "maps.json")
    monkeypatch.setattr(mod, "_MAP_COUNTS_CACHE_FILE", tmp_path / "counts.json")
    mod._ALL_MAPS_CACHE_FILE.write_text(
        json.dumps(
            {
                "fetched_at": "2026-06-20T16:34:00Z",
                "maps": [
                    {
                        "beatsaverCode": "old01",
                        "id": "map-old-01",
                        "songHash": "OLDHASH0123456789",
                        "songName": "Old Song",
                        "songAuthor": "Artist",
                        "songSubName": "",
                        "mapAuthor": "Mapper",
                        "difficulties": [
                            {
                                "id": "diff-old-01",
                                "categoryId": mod.CATEGORY_IDS["true"],
                                "difficulty": "EASY",
                                "characteristic": "Standard",
                                "status": "RANKED",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    maps = mod.load_all_maps_with_recent_batch_fallback(session=cast(requests.Session, _FakeSession()))

    assert maps is not None
    assert len(maps) == 2
    assert {str(song.get("beatsaverCode")) for song in maps} == {"old01", "new01"}
    assert mod.get_reloaded_map_counts_from_cache() == {
        "true": 1,
        "standard": 1,
        "overall": 2,
    }
