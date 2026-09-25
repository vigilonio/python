"""Background-job monitoring: report one execution of a job as a Vigilon Job Run."""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Callable, Iterator
from contextvars import Token
from dataclasses import dataclass
from functools import wraps
from types import TracebackType
from typing import Any, TypeVar, cast

from opentelemetry import context as context_api
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import Span as SdkSpan
from opentelemetry.sdk.trace import SpanProcessor
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

# Public ingest contract. These names are deliberately vendor-neutral so that
# telemetry emitted without this SDK — plain OpenTelemetry, another language —
# still lands on the Vigilon jobs surface.
JOB_NAME_ATTRIBUTE = "job.name"
JOB_SCHEDULE_ATTRIBUTE = "job.schedule"

_JOB_CONTEXT_KEY = context_api.create_key("vigilon.job")
_TRACER_NAME = "vigilon"

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class _JobContextValue:
    name: str
    schedule: str | None


class with_job_monitor:
    """Report executions of a background job as Vigilon Job Runs.

    Works both as a decorator (sync or async functions) and as a context
    manager. Each run starts a new root trace — never attached to ambient
    context — so a job triggered inside some other traced flow still appears
    as its own run. Errors are recorded on the run and re-raised; the return
    value passes through unchanged, so wrapping a job never changes its
    behaviour.

    ``name`` is a stable, code-defined job identifier. Never interpolate
    per-item values (``sync-user-42``); that fragments one job into unbounded
    identities. ``schedule`` is an optional cron expression, enabling last-run
    and stale-job detection.

    Safe to use before ``register()``: the global API returns a no-op tracer,
    and the wrapped function still runs. For concurrent runs, use the
    decorator form or one instance per run — the context-manager form tracks
    nesting, not interleaving.
    """

    def __init__(self, *, name: str, schedule: str | None = None) -> None:
        self._name = name
        self._schedule = schedule
        self._runs: list[contextlib.AbstractContextManager[Span]] = []

    def __call__(self, fn: F) -> F:
        if inspect.iscoroutinefunction(fn):

            @wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with self._run():
                    return await fn(*args, **kwargs)

            return cast(F, async_wrapper)

        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with self._run():
                return fn(*args, **kwargs)

        return cast(F, wrapper)

    def __enter__(self) -> Span:
        run = self._run()
        span = run.__enter__()
        self._runs.append(run)
        return span

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._runs.pop().__exit__(exc_type, exc, tb)

    @contextlib.contextmanager
    def _run(self) -> Iterator[Span]:
        """One job run: a root span with its context attached for the duration.

        Failures are recorded on the run and re-raised; the span always ends
        and the context always detaches, whatever the outcome.
        """
        span, token = self._start_run()
        try:
            yield span
        except Exception as error:
            _fail_span(span, error)
            raise
        finally:
            context_api.detach(token)
            span.end()

    def _start_run(self) -> tuple[Span, Token[Context]]:
        tracer = trace.get_tracer(_TRACER_NAME)

        attributes = {JOB_NAME_ATTRIBUTE: self._name}
        if self._schedule is not None:
            attributes[JOB_SCHEDULE_ATTRIBUTE] = self._schedule

        # An explicit empty Context() parent: never inherits ambient context,
        # so every run is the root of its own trace.
        span = tracer.start_span(
            f"job {self._name}",
            context=Context(),
            kind=SpanKind.INTERNAL,
            attributes=attributes,
        )

        run_context = trace.set_span_in_context(span, Context())
        run_context = context_api.set_value(
            _JOB_CONTEXT_KEY,
            _JobContextValue(name=self._name, schedule=self._schedule),
            context=run_context,
        )
        token = context_api.attach(run_context)
        return span, token


def _fail_span(span: Span, error: BaseException) -> None:
    span.record_exception(error)
    span.set_status(Status(StatusCode.ERROR, str(error) or type(error).__name__))


class JobAttributesSpanProcessor(SpanProcessor):
    """Stamps job attributes onto every span started inside a job run.

    This is required for correctness, not cosmetics: the collector decides
    whether to keep a trace shortly after its first span arrives, but a job's
    root span is only exported when the job ends. Child spans carrying the job
    marker export during execution, so the "keep all job traces" policy
    matches within the decision window even for long-running jobs.
    """

    def on_start(self, span: SdkSpan, parent_context: Context | None = None) -> None:
        job = context_api.get_value(_JOB_CONTEXT_KEY, context=parent_context)
        if not isinstance(job, _JobContextValue):
            return

        span.set_attribute(JOB_NAME_ATTRIBUTE, job.name)
        if job.schedule is not None:
            span.set_attribute(JOB_SCHEDULE_ATTRIBUTE, job.schedule)
