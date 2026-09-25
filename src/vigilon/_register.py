"""Vigilon SDK bootstrap: OTLP export wiring plus auto-instrumentation."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import import_module
from importlib.util import find_spec

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanProcessor

from .jobs import JobAttributesSpanProcessor

_logger = logging.getLogger("vigilon")

DEFAULT_OTEL_ENDPOINT = "https://ingest.vigilon.io"

# Health checks and static noise excluded by default. Patterns are regexes
# matched with re.search against the full request URL (opentelemetry-util-http
# semantics), which is why they anchor on the end of the path, not the start.
DEFAULT_EXCLUDED_URLS = ("/health$", "/ready$", "/favicon\\.ico$")

# The zero-code bootstrap (vigilon._distro) and an in-process register() call
# must not start two SDKs; the guard makes the second call warn and no-op.
_registered = False


@dataclass(frozen=True)
class SpanProcessorContext:
    """Handles passed to the ``extend_span_processors`` seam."""

    batch_span_processor: SpanProcessor
    """The batch processor that feeds the OTLP trace exporter."""


@dataclass(frozen=True)
class SpanProcessorExtensions:
    """Processors contributed by platform/framework packages around the batch exporter."""

    prepend: Sequence[SpanProcessor] = ()
    """Run before the batch processor — for processors that mutate span
    attributes, so the enriched span is what gets enqueued for export."""

    append: Sequence[SpanProcessor] = ()
    """Run after the batch processor — for flush/export hooks that must see
    the ending span already enqueued."""


def register(
    *,
    api_key: str,
    service_name: str,
    environment: str,
    service_version: str | None = None,
    otel_endpoint: str | None = None,
    excluded_urls: Sequence[str] | None = None,
    extend_span_processors: Callable[[SpanProcessorContext], SpanProcessorExtensions] | None = None,
) -> TracerProvider | None:
    """Start the Vigilon SDK: OTLP trace export plus auto-instrumentation.

    Call this as the first statement of your application entrypoint, before
    application modules import the frameworks being instrumented. In pre-fork
    servers (gunicorn, uWSGI, Celery prefork) call it once per worker process,
    inside the worker-startup hook.

    ``excluded_urls`` takes extra URL regexes that are merged with the
    defaults (health checks); use it to exclude instrumentation for APIs you don't want
    tracked/instrumented. ``VIGILON_EXCLUDED_URLS`` (comma-separated) is
    merged the same way.

    Idempotent: if Vigilon has already been started in this process, the call
    warns and returns ``None`` instead of starting a second SDK.
    """
    return _guarded_start(
        api_key=api_key,
        service_name=service_name,
        environment=environment,
        service_version=service_version,
        otel_endpoint=otel_endpoint,
        excluded_urls=excluded_urls,
        extend_span_processors=extend_span_processors,
        instrument=True,
    )


_REQUIRED_ENV_VARS = ("VIGILON_API_KEY", "VIGILON_SERVICE_NAME", "VIGILON_ENVIRONMENT")


def _register_from_environment() -> TracerProvider | None:
    """Env-driven start for the zero-code bootstrap (``vigilon._distro``).

    Skips ``_instrument()``: under ``opentelemetry-instrument`` the
    auto-instrumentation entry-point loop activates every installed
    instrumentation right after the configurator runs, so instrumenting here
    as well would instrument everything twice.
    """
    values = {name: (os.environ.get(name) or "").strip() for name in _REQUIRED_ENV_VARS}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            "The Vigilon zero-code bootstrap requires the VIGILON_API_KEY, "
            "VIGILON_SERVICE_NAME, and VIGILON_ENVIRONMENT environment variables; "
            f"missing: {', '.join(missing)}."
        )

    service_version = (os.environ.get("VIGILON_SERVICE_VERSION") or "").strip() or None
    if service_version is None:
        _logger.warning(
            "Vigilon recommends setting VIGILON_SERVICE_VERSION to track new "
            "deployments and generate better insights."
        )

    return _guarded_start(
        api_key=values["VIGILON_API_KEY"],
        service_name=values["VIGILON_SERVICE_NAME"],
        environment=values["VIGILON_ENVIRONMENT"],
        service_version=service_version,
        otel_endpoint=None,  # VIGILON_OTEL_ENDPOINT is read by _resolve_endpoint
        excluded_urls=None,  # VIGILON_EXCLUDED_URLS is folded in by the distro
        extend_span_processors=None,
        instrument=False,
    )


def _guarded_start(
    *,
    api_key: str,
    service_name: str,
    environment: str,
    service_version: str | None,
    otel_endpoint: str | None,
    excluded_urls: Sequence[str] | None,
    extend_span_processors: Callable[[SpanProcessorContext], SpanProcessorExtensions] | None,
    instrument: bool,
) -> TracerProvider | None:
    global _registered
    if _registered:
        _logger.warning(
            "Vigilon is already registered; ignoring this register() call. "
            "Keep a single register() call per process."
        )
        return None
    _registered = True

    try:
        return _start_sdk(
            api_key=api_key,
            service_name=service_name,
            environment=environment,
            service_version=service_version,
            otel_endpoint=otel_endpoint,
            excluded_urls=excluded_urls,
            extend_span_processors=extend_span_processors,
            instrument=instrument,
        )
    except BaseException:
        # A failed start must not poison the guard — leaving it set would make
        # every later register() attempt warn and no-op.
        _registered = False
        raise


def _start_sdk(
    *,
    api_key: str,
    service_name: str,
    environment: str,
    service_version: str | None,
    otel_endpoint: str | None,
    excluded_urls: Sequence[str] | None,
    extend_span_processors: Callable[[SpanProcessorContext], SpanProcessorExtensions] | None,
    instrument: bool,
) -> TracerProvider:
    _ensure_http_semconv_opt_in()

    endpoint = _resolve_endpoint(otel_endpoint)

    resource_attributes = {
        "service.name": service_name,
        "deployment.environment": environment,
    }
    if service_version is not None:
        resource_attributes["service.version"] = service_version

    trace_exporter = OTLPSpanExporter(
        endpoint=f"{endpoint}/v1/traces",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    batch_span_processor = BatchSpanProcessor(trace_exporter)

    extensions = (
        extend_span_processors(SpanProcessorContext(batch_span_processor=batch_span_processor))
        if extend_span_processors is not None
        else SpanProcessorExtensions()
    )

    provider = TracerProvider(resource=Resource.create(resource_attributes))
    for processor in (
        JobAttributesSpanProcessor(),
        *extensions.prepend,
        batch_span_processor,
        *extensions.append,
    ):
        provider.add_span_processor(processor)

    trace.set_tracer_provider(provider)

    if instrument:
        _instrument(excluded_urls=_merge_excluded_urls(excluded_urls))

    return provider


def _ensure_http_semconv_opt_in() -> None:
    """Guarantee the stable-HTTP semconv opt-in before any instrumentation initializes.

    The mode is latched once per process, and the collector's spanmetrics are
    keyed on the new (stable) HTTP attribute names — without ``http`` in the
    opt-in, requests vanish from the product surfaces. Existing opt-ins (e.g.
    ``database``, or an explicit ``http/dup``) are preserved, not replaced.
    """
    existing = os.environ.get("OTEL_SEMCONV_STABILITY_OPT_IN", "")
    opt_ins = [token.strip() for token in existing.split(",") if token.strip()]
    if not any(token in ("http", "http/dup") for token in opt_ins):
        opt_ins.append("http")
    os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] = ",".join(opt_ins)


def _resolve_endpoint(endpoint: str | None) -> str:
    resolved = (endpoint or os.environ.get("VIGILON_OTEL_ENDPOINT") or "").strip().rstrip("/")
    return resolved or DEFAULT_OTEL_ENDPOINT


def _merge_excluded_urls(extra: Sequence[str] | None) -> str:
    from_env = os.environ.get("VIGILON_EXCLUDED_URLS", "")
    patterns = [
        *DEFAULT_EXCLUDED_URLS,
        *(pattern.strip() for pattern in from_env.split(",")),
        *(extra or ()),
    ]
    return ",".join(dict.fromkeys(pattern for pattern in patterns if pattern))


# (target module to look for, instrumentor module, instrumentor class).
# MySQL has one instrumentation per driver; queries made through a SQLAlchemy
# engine are additionally covered by the SQLAlchemy instrumentation regardless
# of driver (including async drivers, which have no driver-level
# instrumentation upstream).
_INSTRUMENTORS: tuple[tuple[str, str, str], ...] = (
    ("requests", "opentelemetry.instrumentation.requests", "RequestsInstrumentor"),
    ("httpx", "opentelemetry.instrumentation.httpx", "HTTPXClientInstrumentor"),
    ("urllib3", "opentelemetry.instrumentation.urllib3", "URLLib3Instrumentor"),
    ("aiohttp", "opentelemetry.instrumentation.aiohttp_client", "AioHttpClientInstrumentor"),
    ("psycopg", "opentelemetry.instrumentation.psycopg", "PsycopgInstrumentor"),
    ("psycopg2", "opentelemetry.instrumentation.psycopg2", "Psycopg2Instrumentor"),
    ("mysql.connector", "opentelemetry.instrumentation.mysql", "MySQLInstrumentor"),
    ("pymysql", "opentelemetry.instrumentation.pymysql", "PyMySQLInstrumentor"),
    ("MySQLdb", "opentelemetry.instrumentation.mysqlclient", "MySQLClientInstrumentor"),
    ("redis", "opentelemetry.instrumentation.redis", "RedisInstrumentor"),
    ("pymongo", "opentelemetry.instrumentation.pymongo", "PymongoInstrumentor"),
    ("sqlalchemy", "opentelemetry.instrumentation.sqlalchemy", "SQLAlchemyInstrumentor"),
)


def _target_installed(module: str) -> bool:
    try:
        return find_spec(module) is not None
    except ModuleNotFoundError:  # a dotted target whose parent package is absent
        return False


# The two conditional activations below are shared with the zero-code path
# (vigilon._distro.load_instrumentor) — one predicate each, so the programmatic
# and zero-code gates cannot drift apart.


def _should_instrument_django() -> bool:
    """Django is gated on DJANGO_SETTINGS_MODULE, not just the install.

    Without resolvable settings the instrumentation falls back to
    ``settings.configure()`` with *empty* settings, which latches and breaks an
    app that points DJANGO_SETTINGS_MODULE at its real settings later — and
    would needlessly configure Django in apps that merely have it installed.
    """
    return bool(os.environ.get("DJANGO_SETTINGS_MODULE"))


def _should_instrument_aws_lambda() -> bool:
    """Only inside the Lambda runtime.

    There, the container freezes between invocations, which kills the
    BatchSpanProcessor drain; the Lambda instrumentation force-flushes the
    tracer provider at the end of every invocation. Anywhere else it has
    nothing to wrap and just logs noise.
    """
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def _instrument(*, excluded_urls: str) -> None:
    # Some instrumentor modules import their target library at import time, so
    # every instrumentor import stays behind an installed-check on the target.
    if _target_installed("fastapi"):
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor().instrument(excluded_urls=excluded_urls)

    if _target_installed("flask"):
        from opentelemetry.instrumentation.flask import FlaskInstrumentor

        FlaskInstrumentor().instrument(excluded_urls=excluded_urls)

    if _target_installed("django") and _should_instrument_django():
        from opentelemetry.instrumentation.django import DjangoInstrumentor

        DjangoInstrumentor().instrument(excluded_urls=excluded_urls)

    for target, module_name, class_name in _INSTRUMENTORS:
        if not _target_installed(target):
            continue
        instrumentor_class = getattr(import_module(module_name), class_name)
        instrumentor_class().instrument()

    if _should_instrument_aws_lambda():
        from opentelemetry.instrumentation.aws_lambda import AwsLambdaInstrumentor

        AwsLambdaInstrumentor().instrument()
