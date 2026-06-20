from collections.abc import Iterable

# Public persistence secret guard (design D5 / COA-69). This is a dependency-free
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
