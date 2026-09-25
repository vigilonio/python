"""Wheel smoke test: register() must drive one OTLP trace POST to a collector.

Run inside a clean venv that has only the built wheel installed, so it proves
the *packaged* SDK — metadata, entry points, pinned dependencies — not the
source tree:

    uv build
    uv venv .smoke-venv
    uv pip install --python .smoke-venv dist/*.whl
    .smoke-venv/bin/python scripts/smoke_test.py

Asserts the full export path: the Bearer header on the wire, the /v1/traces
route, and a decodable protobuf payload carrying the job span.
"""

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar


class FakeCollector(BaseHTTPRequestHandler):
    exports: ClassVar[list[dict[str, object]]] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).exports.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body": body,
            }
        )
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def fail(message):
    print(f"SMOKE FAILED: {message}", file=sys.stderr)
    raise SystemExit(1)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeCollector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}"

    import vigilon

    provider = vigilon.register(
        api_key="smoke-key",
        service_name="smoke-service",
        environment="smoke",
        service_version="0.0.0",
        otel_endpoint=endpoint,
    )
    if provider is None:
        fail("register() returned None on its first call")

    with vigilon.with_job_monitor(name="smoke"):
        pass

    if not provider.force_flush():
        fail("force_flush timed out")
    server.shutdown()

    if len(FakeCollector.exports) != 1:
        fail(f"expected exactly one export POST, saw {len(FakeCollector.exports)}")
    request = FakeCollector.exports[0]
    if request["path"] != "/v1/traces":
        fail(f"export went to {request['path']}, expected /v1/traces")
    if request["authorization"] != "Bearer smoke-key":
        fail(f"Authorization header was {request['authorization']!r}")
    if request["content_type"] != "application/x-protobuf":
        fail(f"Content-Type was {request['content_type']!r}")

    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    export = ExportTraceServiceRequest()
    export.ParseFromString(request["body"])
    resource_attributes = {
        kv.key: kv.value.string_value
        for rs in export.resource_spans
        for kv in rs.resource.attributes
    }
    if resource_attributes.get("service.name") != "smoke-service":
        fail(f"resource attributes were {resource_attributes}")
    spans = [span for rs in export.resource_spans for ss in rs.scope_spans for span in ss.spans]
    if [span.name for span in spans] != ["job smoke"]:
        fail(f"expected the single span 'job smoke', got {[s.name for s in spans]}")
    job_name = {kv.key: kv.value.string_value for kv in spans[0].attributes}.get("job.name")
    if job_name != "smoke":
        fail(f"job.name attribute was {job_name!r}")

    print(
        "smoke test passed: one protobuf POST to /v1/traces with the Bearer "
        "header, carrying span 'job smoke' from service 'smoke-service'"
    )


if __name__ == "__main__":
    main()
