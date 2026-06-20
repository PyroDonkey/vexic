"""Shared text/list micro-utilities.

Small, generic helpers used by Vexic retrieval, context, and host adapters.
Pure stdlib; imports nothing from the rest of the package.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from typing import Final

HISTORY_TOKEN_BUDGET: Final[int] = 24_000
TAU_SOFT: Final[int] = 18_000

__all__: list[str] = [
    "HISTORY_TOKEN_BUDGET",
    "TAU_SOFT",
    "_collapse_ws",
    "_ordered_unique",
    "estimate_tokens",
]


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def _ordered_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = value.strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _collapse_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
