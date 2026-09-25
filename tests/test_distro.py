import logging
import os
from importlib.metadata import entry_points
from typing import ClassVar

import pytest
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from vigilon import register
from vigilon._distro import VigilonConfigurator, VigilonDistro

DEFAULTS = "/health$,/ready$,/favicon\\.ico$"

EXCLUDED_URL_ENV_VARS = (
    "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS",
    "OTEL_PYTHON_FLASK_EXCLUDED_URLS",
    "OTEL_PYTHON_DJANGO_EXCLUDED_URLS",
)


@pytest.fixture
def clean_env(monkeypatch):
    for var in (
        "OTEL_SEMCONV_STABILITY_OPT_IN",
        "OTEL_PYTHON_EXCLUDED_URLS",
        "VIGILON_EXCLUDED_URLS",
        "VIGILON_API_KEY",
        "VIGILON_SERVICE_NAME",
        "VIGILON_ENVIRONMENT",
        "VIGILON_SERVICE_VERSION",
        "VIGILON_OTEL_ENDPOINT",
        *EXCLUDED_URL_ENV_VARS,
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def test_the_zero_code_entry_points_are_registered():
    # opentelemetry-instrument discovers vendors through these groups; the
    # names must survive packaging, not just exist in source.
    distros = {ep.name: ep.value for ep in entry_points(group="opentelemetry_distro")}
    configurators = {ep.name: ep.value for ep in entry_points(group="opentelemetry_configurator")}

    assert distros["vigilon"] == "vigilon._distro:VigilonDistro"
    assert configurators["vigilon"] == "vigilon._distro:VigilonConfigurator"


class TestVigilonDistro:
    def test_configure_opts_into_stable_http_semconv(self, clean_env):
        VigilonDistro()._configure()

        assert os.environ["OTEL_SEMCONV_STABILITY_OPT_IN"] == "http"

    def test_configure_sets_the_default_excluded_urls_for_every_framework(self, clean_env):
        VigilonDistro()._configure()

        for var in EXCLUDED_URL_ENV_VARS:
            assert os.environ[var] == DEFAULTS

    def test_configure_merges_vigilon_and_framework_specific_patterns(self, clean_env):
        clean_env.setenv("VIGILON_EXCLUDED_URLS", "/internal/health$")
        clean_env.setenv("OTEL_PYTHON_FASTAPI_EXCLUDED_URLS", "/custom$")

        VigilonDistro()._configure()

        assert (
            os.environ["OTEL_PYTHON_FASTAPI_EXCLUDED_URLS"]
            == f"{DEFAULTS},/internal/health$,/custom$"
        )
        assert os.environ["OTEL_PYTHON_FLASK_EXCLUDED_URLS"] == f"{DEFAULTS},/internal/health$"

    def test_configure_folds_the_generic_variable_into_the_specific_ones(self, clean_env):
        # Writing per-framework variables shadows OTEL_PYTHON_EXCLUDED_URLS,
        # so a user's generic value must be carried over, not lost.
        clean_env.setenv("OTEL_PYTHON_EXCLUDED_URLS", "/generic$")

        VigilonDistro()._configure()

        for var in EXCLUDED_URL_ENV_VARS:
            assert os.environ[var] == f"{DEFAULTS},/generic$"


class _RecordingInstrumentor:
    calls: ClassVar[list[dict[str, object]]] = []

    def instrument(self, **kwargs: object) -> None:
        type(self).calls.append(kwargs)


class _FakeEntryPoint:
    def __init__(self, name: str) -> None:
        self.name = name

    def load(self) -> type[_RecordingInstrumentor]:
        return _RecordingInstrumentor


@pytest.fixture
def instrumented_calls():
    _RecordingInstrumentor.calls = []
    return _RecordingInstrumentor.calls


class TestLoadInstrumentor:
    def test_loads_ordinary_instrumentations_and_passes_kwargs(self, instrumented_calls):
        VigilonDistro().load_instrumentor(_FakeEntryPoint("requests"), skip_dep_check=True)

        assert instrumented_calls == [{"skip_dep_check": True}]

    def test_skips_aws_lambda_outside_the_lambda_runtime(self, clean_env, instrumented_calls):
        clean_env.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)

        VigilonDistro().load_instrumentor(_FakeEntryPoint("aws-lambda"))

        assert instrumented_calls == []

    def test_loads_aws_lambda_inside_the_lambda_runtime(self, clean_env, instrumented_calls):
        clean_env.setenv("AWS_LAMBDA_FUNCTION_NAME", "fn")

        VigilonDistro().load_instrumentor(_FakeEntryPoint("aws-lambda"))

        assert instrumented_calls == [{}]

    def test_skips_django_without_a_settings_module_and_warns(
        self, clean_env, instrumented_calls, caplog
    ):
        clean_env.delenv("DJANGO_SETTINGS_MODULE", raising=False)

        with caplog.at_level(logging.WARNING, logger="vigilon"):
            VigilonDistro().load_instrumentor(_FakeEntryPoint("django"))

        assert instrumented_calls == []
        assert "DJANGO_SETTINGS_MODULE" in caplog.text

    def test_loads_django_when_a_settings_module_is_configured(self, clean_env, instrumented_calls):
        clean_env.setenv("DJANGO_SETTINGS_MODULE", "django_settings")

        VigilonDistro().load_instrumentor(_FakeEntryPoint("django"))

        assert instrumented_calls == [{}]


class TestVigilonConfigurator:
    @pytest.fixture
    def env(self, clean_env):
        clean_env.setenv("VIGILON_API_KEY", "secret")
        clean_env.setenv("VIGILON_SERVICE_NAME", "svc")
        clean_env.setenv("VIGILON_ENVIRONMENT", "production")
        clean_env.setenv("VIGILON_SERVICE_VERSION", "1.2.3")
        return clean_env

    def test_builds_the_vigilon_pipeline_from_the_environment(self, registration, env):
        env.setenv("VIGILON_OTEL_ENDPOINT", "http://collector:4318")

        VigilonConfigurator().configure(auto_instrumentation_version="0.65b0")

        provider = registration["provider"]
        attributes = provider.resource.attributes
        assert attributes["service.name"] == "svc"
        assert attributes["deployment.environment"] == "production"
        assert attributes["service.version"] == "1.2.3"
        exporter = next(
            p
            for p in provider._active_span_processor._span_processors
            if isinstance(p, BatchSpanProcessor)
        ).span_exporter
        assert exporter._endpoint == "http://collector:4318/v1/traces"
        assert exporter._headers["Authorization"] == "Bearer secret"

    def test_does_not_instrument(self, registration, env):
        # Under opentelemetry-instrument the entry-point loop instruments right
        # after the configurator; instrumenting here too would double up.
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        VigilonConfigurator().configure()

        assert not FastAPIInstrumentor().is_instrumented_by_opentelemetry

    def test_warns_when_the_service_version_is_missing(self, registration, env, caplog):
        env.delenv("VIGILON_SERVICE_VERSION")

        with caplog.at_level(logging.WARNING, logger="vigilon"):
            VigilonConfigurator().configure()

        assert "VIGILON_SERVICE_VERSION" in caplog.text
        assert "service.version" not in registration["provider"].resource.attributes

    def test_requires_the_vigilon_environment_variables(self, registration, clean_env):
        with pytest.raises(RuntimeError, match="VIGILON_API_KEY"):
            VigilonConfigurator().configure()

        # A refused configure must not poison the guard for register().
        assert register(api_key="key", service_name="svc", environment="dev") is not None

    def test_a_register_call_after_the_configurator_warns_and_noops(
        self, registration, env, caplog
    ):
        VigilonConfigurator().configure()

        with caplog.at_level(logging.WARNING, logger="vigilon"):
            second = register(api_key="key", service_name="svc", environment="dev")

        assert second is None
        assert "already registered" in caplog.text
