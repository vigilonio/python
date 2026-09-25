from opentelemetry.trace import SpanKind, StatusCode
from starlette.testclient import TestClient

from vigilon import record_error, register


def server_spans(exporter):
    return [s for s in exporter.get_finished_spans() if s.kind is SpanKind.SERVER]


def make_app():
    # Resolve FastAPI through the module attribute *after* register() so the
    # instrumented (swapped) class is picked up — the documented ordering rule.
    import fastapi

    app = fastapi.FastAPI()

    @app.get("/hello")
    def hello():
        return {"ok": True}

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/stream")
    def stream():
        return {"ok": True}

    @app.get("/handled-failure")
    def handled_failure(response: fastapi.Response):
        try:
            raise RuntimeError("caught downstream failure")
        except RuntimeError as error:
            record_error(error)
            response.status_code = 500
            return {"message": "failed"}

    return app


def test_requests_produce_new_semconv_server_spans_and_skip_excluded_urls(registration, exporter):
    register(
        api_key="key",
        service_name="svc",
        environment="dev",
        excluded_urls=["/stream$"],
    )

    with TestClient(make_app()) as client:
        assert client.get("/hello").status_code == 200
        # Default and user-supplied exclusions produce no server span at all.
        assert client.get("/health").status_code == 200
        assert client.get("/stream").status_code == 200

    spans = server_spans(exporter)
    assert [s.name for s in spans] == ["GET /hello"]
    attributes = spans[0].attributes
    # The collector's spanmetrics are keyed on these exact (new-semconv)
    # attribute names — this is the load-bearing part of the semconv opt-in.
    assert attributes["http.request.method"] == "GET"
    assert attributes["http.route"] == "/hello"
    assert attributes["http.response.status_code"] == 200


def test_record_error_attaches_the_exception_to_the_server_span(registration, exporter):
    register(api_key="key", service_name="svc", environment="dev")

    with TestClient(make_app()) as client:
        assert client.get("/handled-failure").status_code == 500

    span = server_spans(exporter)[0]
    events = [event for event in span.events if event.name == "exception"]
    assert events, "record_error did not reach the server span"
    assert events[0].attributes["exception.message"] == "caught downstream failure"
    assert span.status.status_code is StatusCode.ERROR
