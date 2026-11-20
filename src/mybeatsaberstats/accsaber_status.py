"""Snapshot-local acquisition status; never infer history from current settings."""


def status_text(snap):
    status = getattr(snap, "accsaber_fetch_status", None)
    labels = {"ok": "取得済み", "skipped": "取得OFF", "missing_id": "取得不可（IDなし）",
              "failed": "取得失敗", "partial": "一部未取得", "not_registered": "未登録"}
    if status in labels:
        return labels[status]
    if any(getattr(snap, f"accsaber_reloaded_{cat}_ap", None) is not None
           for cat in ("overall", "true", "standard", "tech")):
        return "保存済みデータ"
    return "未取得（旧Snapshot・理由の記録なし）"
