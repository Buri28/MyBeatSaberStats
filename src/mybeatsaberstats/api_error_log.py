from __future__ import annotations

from datetime import datetime
import traceback

from .snapshot import BASE_DIR


def log_api_failure(service: str, api_name: str, message: str, exc: BaseException | None = None) -> None:
    log_dir = BASE_DIR / "logs"
    log_path = log_dir / f"{service}_api.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            f"[{datetime.now().isoformat(timespec='seconds')}] {api_name}",
            f"message: {message}",
        ]
        if exc is not None:
            lines.append(f"error: {exc.__class__.__name__}: {exc}")
            lines.append("traceback:")
            lines.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip())
        lines.append("")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception:
        pass