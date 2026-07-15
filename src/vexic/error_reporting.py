import traceback

# Content-free error rendering for persisted diagnostics. Dependency-free
# leaf, like vexic.redaction: dream phases and storage writers can record what
# failed without recording what the user said.


def format_error_detail(exc: BaseException) -> str:
    """Render an exception as its type plus a content-free stack shape.

    Deliberately excludes ``str(exc)``: exception messages routinely embed
    payload content (candidate fact text, validation input, row values), and
    this string is persisted to ``dream_runs.error_detail`` and printed to
    operator logs. Stack frames carry file/line/function and source text only.
    """
    frames = traceback.extract_tb(exc.__traceback__)
    stack = "".join(frames.format())
    rendered = f"{type(exc).__name__}\n{stack}".rstrip()
    return rendered


# Marks whether a dream phase's terminal ``dream_runs`` error row was durably
# persisted before the phase re-raised. The sweeper reads it to decide
# whether advancing the 24h retry clock would hide the failure: advancing is only
# safe once the failure is queryable. The attribute rides the raised exception so
# the phase's public raise type is unchanged (CLI and direct callers ignore it).
_DREAM_RECORDED_ATTR = "_vexic_dream_failure_recorded"


def mark_dream_recorded(exc: BaseException, recorded: bool) -> BaseException:
    """Tag a dream-phase exception with whether its error row was recorded.

    Returns ``exc`` so callers can ``raise mark_dream_recorded(exc, recorded)``.
    """
    setattr(exc, _DREAM_RECORDED_ATTR, bool(recorded))
    return exc


def dream_failure_recorded(exc: BaseException) -> bool:
    """True only when a phase explicitly marked its failure durably recorded.

    Walks the ``__cause__`` chain so the mark survives an explicit re-wrap: the
    hosted dream boundary rewraps a phase's ``NotImplementedError`` as
    ``HostPortNotConfigured`` via ``raise ... from exc``, and the marked original
    is preserved on ``__cause__``. Reading only the outer exception would drop
    the mark and treat a durably-recorded failure as unrecorded -- re-dreaming
    every tick and burning model spend.

    Defaults ``False`` so an exception raised before any phase writer ran (e.g.
    a tenant-DB fault in ``init_schema``/authorization) is treated as unrecorded,
    and the sweeper declines to advance the retry clock rather than hide it.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if getattr(current, _DREAM_RECORDED_ATTR, False):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False
