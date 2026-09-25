"""End-to-end test of the zero-code bootstrap.

A subprocess runs under ``opentelemetry-instrument`` with only ``VIGILON_*``
environment variables and no ``vigilon`` import anywhere in the app code. Its
telemetry must arrive at a local fake collector: proof that the distro and
configurator entry points wire the whole pipeline without touching the app.
"""

import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)

# The app never imports vigilon; the outgoing requests call is the telemetry
# source (the requests instrumentation is activated by the entry-point loop).
APP = """
import requests

requests.get("{endpoint}/ping")
"""


class _FakeCollector(BaseHTTPRequestHandler):
    exports: ClassVar[list[dict[str, object]]] = []

    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).exports.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": body,
            }
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def collector():
    _FakeCollector.exports = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeCollector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_opentelemetry_instrument_exports_traces_with_only_vigilon_env_vars(collector):
    # Strip inherited OTEL_*/VIGILON_* state (earlier tests mutate os.environ)
    # so the subprocess sees exactly what a fresh zero-code deployment would.
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("OTEL_", "VIGILON_"))
    }
    env.update(
        VIGILON_API_KEY="zero-code-key",
        VIGILON_SERVICE_NAME="zero-code-service",
        VIGILON_ENVIRONMENT="test",
        VIGILON_SERVICE_VERSION="0.0.1",
        VIGILON_OTEL_ENDPOINT=collector,
    )

    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "opentelemetry-instrument"),
            sys.executable,
            "-c",
            APP.format(endpoint=collector),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,  # the return code is asserted below, with stderr attached
    )

    assert result.returncode == 0, result.stderr
    exports = [e for e in _FakeCollector.exports if e["path"] == "/v1/traces"]
    assert exports, f"no trace export arrived; stderr: {result.stderr}"
    assert all(e["authorization"] == "Bearer zero-code-key" for e in exports)

    request = ExportTraceServiceRequest()
    request.ParseFromString(b"".join(e["body"] for e in exports))
    resource_attributes = {
        kv.key: kv.value.string_value
        for rs in request.resource_spans
        for kv in rs.resource.attributes
    }
    assert resource_attributes["service.name"] == "zero-code-service"
    assert resource_attributes["deployment.environment"] == "test"
    assert resource_attributes["service.version"] == "0.0.1"

    spans = [span for rs in request.resource_spans for ss in rs.scope_spans for span in ss.spans]
    span_attributes = {kv.key for span in spans for kv in span.attributes}
    # New-semconv attribute names prove the distro's semconv opt-in reached
    # the instrumentation — the collector's spanmetrics depend on them.
    assert "http.request.method" in span_attributes, spans
