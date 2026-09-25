import contextlib

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import vigilon._register as register_module
from vigilon import JobAttributesSpanProcessor

# The global tracer provider can only be set once per process, so the whole
# test session shares one provider wired to an in-memory exporter. register()
# tests monkeypatch trace.set_tracer_provider instead of touching this one.
_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(JobAttributesSpanProcessor())
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    _EXPORTER.clear()
    return _EXPORTER


def _uninstrument_everything() -> None:
    # Only the instrumentations whose target libraries exist in the dev
    # environment actually activate; requests/urllib3 arrive transitively via
    # the OTLP exporter, fastapi/httpx via the dev dependency group.
    from opentelemetry.instrumentation.django import DjangoInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.pymysql import PyMySQLInstrumentor
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor

    for instrumentor in (
        DjangoInstrumentor(),
        FastAPIInstrumentor(),
        FlaskInstrumentor(),
        HTTPXClientInstrumentor(),
        PyMySQLInstrumentor(),
        RequestsInstrumentor(),
        URLLib3Instrumentor(),
    ):
        with contextlib.suppress(Exception):
            instrumentor.uninstrument()


@pytest.fixture
def registration(monkeypatch):
    """Reset the idempotency guard and capture the global-provider hand-off.

    The real trace.set_tracer_provider only works once per process, so tests
    capture the provider instead of setting it globally; instrumentation then
    falls back to the session provider above, keeping spans in-memory.
    """
    monkeypatch.setattr(register_module, "_registered", False)
    captured: dict[str, TracerProvider] = {}
    monkeypatch.setattr(
        register_module.trace,
        "set_tracer_provider",
        lambda provider: captured.__setitem__("provider", provider),
    )
    yield captured
    _uninstrument_everything()
