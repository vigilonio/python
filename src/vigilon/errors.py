"""Record errors on the active span so they surface as Vigilon exceptions."""

from __future__ import annotations

import json

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode


def record_error(error: object) -> None:
    """Record an error on the currently active span.

    Runtime-agnostic: works anywhere there is an active OpenTelemetry span —
    FastAPI route handlers, background jobs (``with_job_monitor``), and Lambda
    invocations — because it reads the active span from context rather than
    any response object. If there is no active span (called outside any traced
    scope), it is a no-op.

    Unhandled exceptions are already recorded by the framework
    instrumentations; call this for errors your code catches and converts into
    an error response itself, which are otherwise invisible to instrumentation.
    """
    span = trace.get_current_span()
    if not span.is_recording():
        return

    recorded = _to_recorded_exception(error)
    span.record_exception(recorded)
    span.set_status(Status(StatusCode.ERROR, str(recorded) or type(recorded).__name__))


def _to_recorded_exception(error: object) -> BaseException:
    if isinstance(error, BaseException):
        return error
    return Exception(_format_error_message(error))


def _format_error_message(error: object) -> str:
    if isinstance(error, str):
        return error
    try:
        return json.dumps(error, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(error)
