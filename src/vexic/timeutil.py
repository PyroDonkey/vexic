from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Current UTC time as an ISO 8601 string. Shared by pipeline phases so the
    timestamp format cannot drift between Light and Deep runs."""
    return datetime.now(timezone.utc).isoformat()
