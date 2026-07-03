from vexic.error_reporting import format_error_detail


def test_format_error_detail_excludes_exception_message_content() -> None:
    # Exception messages routinely embed payload content (fact text,
    # validation input); persisted diagnostics must carry the type only.
    sentinel = "user-memory-fact-sentinel"
    try:
        raise ValueError(f"validation failed for {sentinel!r}")
    except ValueError as exc:
        detail = format_error_detail(exc)

    assert "ValueError" in detail
    assert sentinel not in detail


def test_format_error_detail_keeps_stack_shape_for_debugging() -> None:
    def inner() -> None:
        raise KeyError("k")

    try:
        inner()
    except KeyError as exc:
        detail = format_error_detail(exc)

    assert "KeyError" in detail
    assert "test_error_reporting.py" in detail
    assert "inner" in detail
