from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from vigilon import record_error


def only_span(exporter: InMemorySpanExporter) -> ReadableSpan:
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    return spans[0]


def exception_event(span: ReadableSpan):
    events = [event for event in span.events if event.name == "exception"]
    assert events, "no exception event recorded"
    return events[0]


def test_records_the_exception_and_error_status_on_the_active_span(exporter):
    with trace.get_tracer("test").start_as_current_span("op"):
        record_error(ValueError("boom"))

    span = only_span(exporter)
    assert span.status.status_code is StatusCode.ERROR
    assert span.status.description == "boom"
    event = exception_event(span)
    assert event.attributes["exception.type"] == "ValueError"
    assert event.attributes["exception.message"] == "boom"


def test_is_a_noop_when_there_is_no_active_span(exporter):
    record_error(ValueError("boom"))

    assert exporter.get_finished_spans() == ()


def test_is_a_noop_on_a_span_that_is_no_longer_recording(exporter):
    with trace.get_tracer("test").start_as_current_span("op", end_on_exit=False) as span:
        span.end()
        record_error(ValueError("boom"))

    span = only_span(exporter)
    assert span.status.status_code is StatusCode.UNSET
    assert [event.name for event in span.events] == []


def test_coerces_a_string_into_an_exception(exporter):
    with trace.get_tracer("test").start_as_current_span("op"):
        record_error("plain string failure")

    span = only_span(exporter)
    assert span.status.description == "plain string failure"
    assert exception_event(span).attributes["exception.message"] == "plain string failure"


def test_coerces_a_plain_object_into_an_exception_via_json(exporter):
    with trace.get_tracer("test").start_as_current_span("op"):
        record_error({"code": 500, "reason": "downstream"})

    message = exception_event(only_span(exporter)).attributes["exception.message"]
    assert message == '{"code":500,"reason":"downstream"}'


def test_falls_back_to_str_when_the_value_is_not_json_serializable(exporter):
    marker = object()
    with trace.get_tracer("test").start_as_current_span("op"):
        record_error(marker)

    message = exception_event(only_span(exporter)).attributes["exception.message"]
    assert message == str(marker)


def test_preserves_the_original_exception_type_rather_than_rewrapping(exporter):
    class CustomError(Exception):
        pass

    with trace.get_tracer("test").start_as_current_span("op"):
        record_error(CustomError("custom"))

    event = exception_event(only_span(exporter))
    assert str(event.attributes["exception.type"]).endswith("CustomError")
    assert event.attributes["exception.message"] == "custom"
