"""Generic configuration helpers (sample_config partial overrides)."""

from typing import Any, Dict


def deep_merge(base: dict, override: dict) -> dict:
    """Recursive dict merge: dicts merge field-by-field; everything else
    (scalars, lists) is replaced wholesale. Used for sample_config partial
    overrides at arbitrary depth."""
    out = dict(base)
    for key, val in override.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out
