import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import INVALID_SPAN, SpanKind, StatusCode

from vigilon import JOB_NAME_ATTRIBUTE, JOB_SCHEDULE_ATTRIBUTE, with_job_monitor


def job_span(exporter: InMemorySpanExporter, name: str = "sync-users") -> ReadableSpan:
    spans = [s for s in exporter.get_finished_spans() if s.name == f"job {name}"]
    assert spans, f"no span found for job {name}"
    return spans[0]


def event_names(span: ReadableSpan) -> list[str]:
    return [event.name for event in span.events]


class TestWithJobMonitor:
    def test_passes_through_a_synchronous_return_value(self, exporter):
        result = with_job_monitor(name="sync-users")(lambda: 42)()

        assert result == 42
        assert len(exporter.get_finished_spans()) == 1

    async def test_passes_through_an_async_result_and_ends_the_span_after_it_settles(
        self, exporter
    ):
        @with_job_monitor(name="sync-users")
        async def job():
            # The span must still be open while the job is running.
            assert len(exporter.get_finished_spans()) == 0
            return "done"

        assert await job() == "done"
        assert len(exporter.get_finished_spans()) == 1

    def test_records_the_span_as_a_job_run_with_a_name_and_no_schedule_by_default(self, exporter):
        with_job_monitor(name="sync-users")(lambda: None)()

        span = job_span(exporter)
        assert span.kind is SpanKind.INTERNAL
        assert span.attributes[JOB_NAME_ATTRIBUTE] == "sync-users"
        assert JOB_SCHEDULE_ATTRIBUTE not in span.attributes
        # Success leaves the status unset, matching HTTP span behaviour.
        assert span.status.status_code is StatusCode.UNSET

    def test_records_the_schedule_when_provided(self, exporter):
        with_job_monitor(name="sync-users", schedule="0 3 * * *")(lambda: None)()

        assert job_span(exporter).attributes[JOB_SCHEDULE_ATTRIBUTE] == "0 3 * * *"

    def test_marks_the_span_failed_and_reraises_when_the_job_raises(self, exporter):
        @with_job_monitor(name="sync-users")
        def job():
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            job()

        span = job_span(exporter)
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description == "boom"
        assert "exception" in event_names(span)

    async def test_marks_the_span_failed_and_reraises_when_an_async_job_raises(self, exporter):
        @with_job_monitor(name="sync-users")
        async def job():
            raise ValueError("async boom")

        with pytest.raises(ValueError, match="async boom"):
            await job()

        span = job_span(exporter)
        assert span.status.status_code is StatusCode.ERROR
        assert "exception" in event_names(span)

    def test_uses_the_exception_type_when_the_message_is_empty(self, exporter):
        @with_job_monitor(name="sync-users")
        def job():
            raise ValueError()

        with pytest.raises(ValueError):
            job()

        assert job_span(exporter).status.description == "ValueError"

    def test_starts_a_new_root_trace_even_inside_an_active_span(self, exporter):
        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("outer") as outer:
            with_job_monitor(name="sync-users")(lambda: None)()

        span = job_span(exporter)
        assert span.parent is None
        assert span.context.trace_id != outer.get_span_context().trace_id

    def test_makes_the_job_span_the_active_span_inside_the_wrapped_function(self, exporter):
        seen = {}

        @with_job_monitor(name="sync-users")
        def job():
            seen["span_id"] = trace.get_current_span().get_span_context().span_id

        job()

        assert seen["span_id"] == job_span(exporter).context.span_id

    def test_detaches_the_job_context_after_the_run(self, exporter):
        with_job_monitor(name="sync-users")(lambda: None)()

        assert trace.get_current_span() is INVALID_SPAN

    def test_context_manager_form_reports_a_run_and_activates_the_span(self, exporter):
        with with_job_monitor(name="sync-users") as span:
            assert trace.get_current_span().get_span_context().span_id == (
                span.get_span_context().span_id
            )

        exported = job_span(exporter)
        assert exported.status.status_code is StatusCode.UNSET
        assert exported.attributes[JOB_NAME_ATTRIBUTE] == "sync-users"

    def test_context_manager_form_records_failures_and_reraises(self, exporter):
        with pytest.raises(ValueError, match="boom"), with_job_monitor(name="sync-users"):
            raise ValueError("boom")

        span = job_span(exporter)
        assert span.status.status_code is StatusCode.ERROR
        assert "exception" in event_names(span)


class TestJobAttributesSpanProcessor:
    def test_stamps_job_attributes_onto_child_spans_started_inside_the_job(self, exporter):
        @with_job_monitor(name="sync-users", schedule="0 3 * * *")
        def job():
            child = trace.get_tracer("test").start_span("db.query")
            child.end()

        job()

        child = next(s for s in exporter.get_finished_spans() if s.name == "db.query")
        assert child.attributes[JOB_NAME_ATTRIBUTE] == "sync-users"
        assert child.attributes[JOB_SCHEDULE_ATTRIBUTE] == "0 3 * * *"
        assert child.context.trace_id == job_span(exporter).context.trace_id

    def test_leaves_spans_started_outside_a_job_untouched(self, exporter):
        span = trace.get_tracer("test").start_span("http.request")
        span.end()

        exported = next(s for s in exporter.get_finished_spans() if s.name == "http.request")
        assert JOB_NAME_ATTRIBUTE not in exported.attributes
