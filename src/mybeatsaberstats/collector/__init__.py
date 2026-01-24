"""Collector package initialiser.

Provide lazy attribute access for commonly-used functions from
`collector.collector` to avoid circular import problems while still
allowing `from .collector import <name>` style imports.
"""

from typing import TYPE_CHECKING

# Public API exposed via package-level import
__all__ = [
    "rebuild_player_index_from_global",
    "ensure_global_rank_caches",
    "collect_beatleader_star_stats",
    "create_snapshot_for_steam_id",
]

if TYPE_CHECKING:
    # Import names only for static analysis/type checkers to avoid runtime import side-effects
    from .collector import (
        rebuild_player_index_from_global,
        ensure_global_rank_caches,
        collect_beatleader_star_stats,
        create_snapshot_for_steam_id,
    )


def __getattr__(name: str):
    if name in __all__:
        # Import submodule only when attribute is requested
        from . import collector as _collector

        try:
            return getattr(_collector, name)
        except AttributeError as exc:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)
