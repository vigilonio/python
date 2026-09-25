import logging

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

import vigilon._register as register_module
from vigilon import (
    DEFAULT_OTEL_ENDPOINT,
    JobAttributesSpanProcessor,
    SpanProcessorExtensions,
    register,
)


def span_processors(provider: TracerProvider):
    return provider._active_span_processor._span_processors


def test_builds_a_provider_with_the_vigilon_resource_attributes(registration):
    provider = register(
        api_key="key",
        service_name="svc",
        environment="production",
        service_version="1.2.3",
    )

    assert provider is registration["provider"]
    attributes = provider.resource.attributes
    assert attributes["service.name"] == "svc"
    assert attributes["deployment.environment"] == "production"
    assert attributes["service.version"] == "1.2.3"


def test_omits_service_version_when_not_provided(registration):
    provider = register(api_key="key", service_name="svc", environment="dev")

    assert "service.version" not in provider.resource.attributes


def test_orders_processors_around_the_batch_processor(registration):
    class Marker(JobAttributesSpanProcessor):
        pass

    prepended, appended = Marker(), Marker()
    seam: dict[str, object] = {}

    def extend(ctx):
        seam["batch"] = ctx.batch_span_processor
        return SpanProcessorExtensions(prepend=[prepended], append=[appended])

    provider = register(
        api_key="key",
        service_name="svc",
        environment="dev",
        extend_span_processors=extend,
    )

    processors = span_processors(provider)
    assert isinstance(processors[0], JobAttributesSpanProcessor)
    assert processors[1] is prepended
    assert isinstance(processors[2], BatchSpanProcessor)
    assert processors[2] is seam["batch"]
    assert processors[3] is appended


def test_sends_the_api_key_as_a_bearer_token_to_the_default_endpoint(registration):
    provider = register(api_key="secret", service_name="svc", environment="dev")

    exporter = next(
        p for p in span_processors(provider) if isinstance(p, BatchSpanProcessor)
    ).span_exporter
    assert exporter._endpoint == f"{DEFAULT_OTEL_ENDPOINT}/v1/traces"
    assert exporter._headers["Authorization"] == "Bearer secret"


def test_strips_trailing_slashes_from_a_custom_endpoint(registration):
    provider = register(
        api_key="key",
        service_name="svc",
        environment="dev",
        otel_endpoint="http://localhost:4318//",
    )

    exporter = next(
        p for p in span_processors(provider) if isinstance(p, BatchSpanProcessor)
    ).span_exporter
    assert exporter._endpoint == "http://localhost:4318/v1/traces"


def test_reads_the_endpoint_from_the_environment_when_not_passed(registration, monkeypatch):
    monkeypatch.setenv("VIGILON_OTEL_ENDPOINT", "http://collector:4318")

    provider = register(api_key="key", service_name="svc", environment="dev")

    exporter = next(
        p for p in span_processors(provider) if isinstance(p, BatchSpanProcessor)
    ).span_exporter
    assert exporter._endpoint == "http://collector:4318/v1/traces"


def test_second_register_call_warns_and_noops(registration, caplog):
    first = register(api_key="key", service_name="svc", environment="dev")

    with caplog.at_level(logging.WARNING, logger="vigilon"):
        second = register(api_key="key", service_name="svc", environment="dev")

    assert first is not None
    assert second is None
    assert "already registered" in caplog.text


def test_a_failed_start_does_not_poison_the_guard(registration):
    def broken_seam(ctx):
        raise RuntimeError("seam exploded")

    with pytest.raises(RuntimeError, match="seam exploded"):
        register(
            api_key="key",
            service_name="svc",
            environment="dev",
            extend_span_processors=broken_seam,
        )

    assert register(api_key="key", service_name="svc", environment="dev") is not None


def test_opts_into_stable_http_semconv(registration, monkeypatch):
    monkeypatch.delenv("OTEL_SEMCONV_STABILITY_OPT_IN", raising=False)

    register(api_key="key", service_name="svc", environment="dev")

    import os

    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http"


def test_respects_a_preexisting_http_dup_opt_in(registration, monkeypatch):
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "http/dup")

    register(api_key="key", service_name="svc", environment="dev")

    import os

    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http/dup"


def test_merges_http_into_an_existing_semconv_opt_in(registration, monkeypatch):
    # A pre-existing opt-in without http (e.g. database) must not disable the
    # stable HTTP attributes the collector's spanmetrics are keyed on.
    monkeypatch.setenv("OTEL_SEMCONV_STABILITY_OPT_IN", "database")

    register(api_key="key", service_name="svc", environment="dev")

    import os

    assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "database,http"


def test_instruments_fastapi_when_installed(registration):
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    register(api_key="key", service_name="svc", environment="dev")

    assert FastAPIInstrumentor().is_instrumented_by_opentelemetry


def test_instruments_pymysql_when_installed(registration):
    from opentelemetry.instrumentation.pymysql import PyMySQLInstrumentor

    register(api_key="key", service_name="svc", environment="dev")

    assert PyMySQLInstrumentor().is_instrumented_by_opentelemetry


def test_instruments_flask_when_installed(registration):
    from opentelemetry.instrumentation.flask import FlaskInstrumentor

    register(api_key="key", service_name="svc", environment="dev")

    assert FlaskInstrumentor().is_instrumented_by_opentelemetry


def test_skips_django_when_no_settings_module_is_configured(registration, monkeypatch):
    # Instrumenting without resolvable settings would latch empty Django
    # settings (the instrumentation calls settings.configure() as a fallback),
    # breaking apps that set DJANGO_SETTINGS_MODULE later.
    from opentelemetry.instrumentation.django import DjangoInstrumentor

    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)

    register(api_key="key", service_name="svc", environment="dev")

    assert not DjangoInstrumentor().is_instrumented_by_opentelemetry


def test_instruments_django_when_a_settings_module_is_configured(registration, monkeypatch):
    from opentelemetry.instrumentation.django import DjangoInstrumentor

    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "django_settings")

    register(api_key="key", service_name="svc", environment="dev")

    assert DjangoInstrumentor().is_instrumented_by_opentelemetry


class TestMergeExcludedUrls:
    def test_defaults_only(self, monkeypatch):
        monkeypatch.delenv("VIGILON_EXCLUDED_URLS", raising=False)

        merged = register_module._merge_excluded_urls(None)

        assert merged == "/health$,/ready$,/favicon\\.ico$"

    def test_merges_env_and_param_extras_after_the_defaults(self, monkeypatch):
        monkeypatch.setenv("VIGILON_EXCLUDED_URLS", "/internal/health$, /events/stream$")

        merged = register_module._merge_excluded_urls(["/live$"])

        assert merged == "/health$,/ready$,/favicon\\.ico$,/internal/health$,/events/stream$,/live$"

    def test_deduplicates_and_drops_empty_entries(self, monkeypatch):
        monkeypatch.setenv("VIGILON_EXCLUDED_URLS", "/health$,, ")

        merged = register_module._merge_excluded_urls(["/ready$"])

        assert merged == "/health$,/ready$,/favicon\\.ico$"
