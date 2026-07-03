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
