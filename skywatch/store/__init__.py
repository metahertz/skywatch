"""Persistence layer.  Currently MongoDB-backed (optional)."""

try:
    from .mongo import MongoStore  # noqa: F401
    HAS_MONGO = True
except ImportError:
    # pymongo not installed; persistence stays disabled at runtime.
    MongoStore = None  # type: ignore[assignment]
    HAS_MONGO = False

__all__ = ["MongoStore", "HAS_MONGO"]
