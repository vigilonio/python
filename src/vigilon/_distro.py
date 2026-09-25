"""Zero-code bootstrap: the entry points behind ``opentelemetry-instrument``.

``opentelemetry-instrument python app.py`` discovers vendors through entry
points and runs them in a fixed order: the distro's ``_configure`` (process-wide
environment defaults), then the configurator (builds the Vigilon trace pipeline
from ``VIGILON_*`` environment variables), then the auto-instrumentation loop,
which activates every installed instrumentation through the distro's
``load_instrumentor`` — where per-instrumentation policy lives.

Fork-model servers (gunicorn, uWSGI, Celery prefork) must not use this path:
a provider built pre-fork loses its exporter thread in the workers. They call
``vigilon.register()`` inside the worker-startup hook instead.
"""

from __future__ import annotations

import logging
import os
from importlib.metadata import EntryPoint
from typing import Any

# The distro module carries a file-level `# type: ignore`, so mypy sees no
# attributes on it at all.
from opentelemetry.instrumentation.distro import BaseDistro  # type: ignore[attr-defined]

from ._register import (
    _ensure_http_semconv_opt_in,
    _merge_excluded_urls,
    _register_from_environment,
    _should_instrument_aws_lambda,
    _should_instrument_django,
)

_logger = logging.getLogger("vigilon")

# The HTTP-server instrumentations read excluded URLs from their per-framework
# variable (falling back to OTEL_PYTHON_EXCLUDED_URLS) at module import time,
# so the merged value must be in place before the instrumentor modules load —
# i.e. in the distro, which runs first.
_EXCLUDED_URL_ENV_VARS = (
    "OTEL_PYTHON_FASTAPI_EXCLUDED_URLS",
    "OTEL_PYTHON_FLASK_EXCLUDED_URLS",
    "OTEL_PYTHON_DJANGO_EXCLUDED_URLS",
)


class VigilonDistro(BaseDistro):
    """Environment defaults and instrumentation policy for the zero-code path."""

    def _configure(self, **kwargs: Any) -> None:
        _ensure_http_semconv_opt_in()

        generic = os.environ.get("OTEL_PYTHON_EXCLUDED_URLS", "")
        for env_var in _EXCLUDED_URL_ENV_VARS:
            # Writing the per-framework variable shadows the generic one, so a
            # user's own value (either variable) is folded into the merge.
            existing = os.environ.get(env_var, generic)
            os.environ[env_var] = _merge_excluded_urls(
                [pattern.strip() for pattern in existing.split(",")]
            )

    def load_instrumentor(self, entry_point: EntryPoint, **kwargs: Any) -> None:
        # Same predicates as the programmatic path (_register._instrument);
        # their docstrings carry the rationale.
        if entry_point.name == "aws-lambda" and not _should_instrument_aws_lambda():
            return
        if entry_point.name == "django" and not _should_instrument_django():
            _logger.warning(
                "Vigilon is not instrumenting Django because DJANGO_SETTINGS_MODULE "
                "is not set. Export it in the environment (not in manage.py) so it "
                "is set before opentelemetry-instrument initializes."
            )
            return
        super().load_instrumentor(entry_point, **kwargs)


class VigilonConfigurator:
    """Builds the Vigilon trace pipeline from ``VIGILON_*`` environment variables.

    Requires ``VIGILON_API_KEY``, ``VIGILON_SERVICE_NAME``, and
    ``VIGILON_ENVIRONMENT``; ``VIGILON_SERVICE_VERSION`` is recommended and
    warns when missing. Instrumentation itself is left to the
    auto-instrumentation loop that runs right after configurators.
    """

    def configure(self, **kwargs: Any) -> None:
        _register_from_environment()
