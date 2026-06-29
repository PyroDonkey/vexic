from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

# Public persistence secret guard. This is a dependency-free
# leaf: Vexic storage and host adapters can scrub text against loaded secret
# values without reaching into host runtime modules.


def assert_no_forbidden_secret_values(
    forbidden_secret_values: Iterable[str],
    *texts: str,
) -> None:
    """Raise if any non-empty forbidden secret value appears in any of `texts`.

    Fail-closed guard for every path that persists model-visible or stored text:
    a loaded secret value must never be written to SQLite/FTS, an approval packet,
    a review sidecar, an HTML artifact, or handed back to a model.
    """
    for secret in forbidden_secret_values:
        if not secret:
            continue
        if any(secret in text for text in texts):
            raise ValueError("Refusing to persist message containing a forbidden secret value.")


def iter_payload_strings(value: Any) -> Iterator[str]:
    """Yield every string nested anywhere inside a JSON-serializable value.

    Walks mappings and sequences so a forbidden-value egress check can cover
    *all* returned string fields, not just a hand-picked subset.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from iter_payload_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            yield from iter_payload_strings(item)


def assert_no_forbidden_secret_values_in_payload(
    forbidden_secret_values: Iterable[str],
    payload: Any,
) -> None:
    """Fail-closed egress guard over every string in a structured payload.

    Build the outgoing payload first, then run this so secrets cannot leak
    through any serialized string field (for example ``LongTermFact.subject``),
    not only the few fields a call site remembers to list.
    """
    assert_no_forbidden_secret_values(
        forbidden_secret_values,
        *iter_payload_strings(payload),
    )
