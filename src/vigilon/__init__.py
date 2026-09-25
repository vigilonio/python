"""Vigilon OpenTelemetry bootstrap for Python apps."""

from importlib.metadata import PackageNotFoundError, version

from ._register import (
    DEFAULT_OTEL_ENDPOINT,
    SpanProcessorContext,
    SpanProcessorExtensions,
    register,
)
from .errors import record_error
from .jobs import (
    JOB_NAME_ATTRIBUTE,
    JOB_SCHEDULE_ATTRIBUTE,
    JobAttributesSpanProcessor,
    with_job_monitor,
)

try:
    __version__ = version("vigilon")
except PackageNotFoundError:  # pragma: no cover — running from a source tree
    __version__ = "0.0.0"

__all__ = [
    "DEFAULT_OTEL_ENDPOINT",
    "JOB_NAME_ATTRIBUTE",
    "JOB_SCHEDULE_ATTRIBUTE",
    "JobAttributesSpanProcessor",
    "SpanProcessorContext",
    "SpanProcessorExtensions",
    "__version__",
    "record_error",
    "register",
    "with_job_monitor",
]
