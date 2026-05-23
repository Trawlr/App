"""
Serialization utilities for converting Telethon objects to JSON-serializable dicts.
"""

from datetime import datetime


def serialize_for_json(obj):
    """Recursively convert Telethon objects to JSON-serializable dicts."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, bytes):
        return f"<bytes len={len(obj)}>"
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (list, tuple)):
        return [serialize_for_json(item) for item in obj]
    if isinstance(obj, dict):
        return {str(k): serialize_for_json(v) for k, v in obj.items()}

    # For Telethon objects, try to get all attributes
    result = {'_type': type(obj).__name__}

    # Try to_dict() first (some Telethon objects have this)
    if hasattr(obj, 'to_dict'):
        try:
            d = obj.to_dict()
            return {**result, **{str(k): serialize_for_json(v) for k, v in d.items()}}
        except Exception:
            pass

    # Otherwise iterate through attributes
    for attr in dir(obj):
        if attr.startswith('_'):
            continue
        try:
            val = getattr(obj, attr)
            if callable(val):
                continue
            result[attr] = serialize_for_json(val)
        except Exception as e:
            result[attr] = f"<error: {e}>"

    return result
