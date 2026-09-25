# `vigilon`

Vigilon OpenTelemetry bootstrap for Python apps: one `register()` call — or the zero-code `opentelemetry-instrument` launcher — wires OTLP trace export to Vigilon and auto-instruments FastAPI, Flask, and Django plus the common HTTP/database clients.

The distribution and import package are both named `vigilon`.

## Install

```bash
pip install vigilon
```

## Quick start

Call `register()` as the **first statement of your entrypoint**, before application modules import the frameworks being instrumented:

```python
import vigilon

vigilon.register(
    api_key="your-api-key",
    service_name="your-service-name",
    environment="production",
    service_version="1.2.3",  # optional, recommended for deploy tracking
)

from app import create_app  # import your app AFTER register()

app = create_app()
```

Optional environment variables:

- `VIGILON_OTEL_ENDPOINT` — override the OTLP HTTP base endpoint (traces go to `<endpoint>/v1/traces`); useful for local collectors and tests.
- `VIGILON_EXCLUDED_URLS` — extra excluded-URL regexes, comma-separated (see below).

## Zero-code bootstrap

If you cannot (or would rather not) touch the entrypoint, launch the app under `opentelemetry-instrument` — the SDK registers itself through OpenTelemetry's distro/configurator hooks, driven entirely by environment variables:

```bash
VIGILON_API_KEY=your-api-key \
VIGILON_SERVICE_NAME=your-service-name \
VIGILON_ENVIRONMENT=production \
VIGILON_SERVICE_VERSION=1.2.3 \
opentelemetry-instrument python app.py
```

| Variable | Meaning |
|---|---|
| `VIGILON_API_KEY` | **Required.** Your Vigilon API key. |
| `VIGILON_SERVICE_NAME` | **Required.** Service name shown in Vigilon. |
| `VIGILON_ENVIRONMENT` | **Required.** Deployment environment (`production`, `staging`, …). |
| `VIGILON_SERVICE_VERSION` | Recommended — enables deploy tracking; the SDK warns when missing. |
| `VIGILON_OTEL_ENDPOINT` | Optional OTLP endpoint override. |
| `VIGILON_EXCLUDED_URLS` | Optional extra excluded-URL regexes, comma-separated. |

Notes:

- **Pre-fork servers (gunicorn, uWSGI, Celery prefork) must not use this path** — the SDK would start before the fork and lose its exporter thread in the workers. Use the per-worker `register()` recipes below instead.
- **Django**: export `DJANGO_SETTINGS_MODULE` in the environment (not inside `manage.py`) — it must be set before `opentelemetry-instrument` initializes, or Django is deliberately left uninstrumented.
- If another OpenTelemetry distro package is installed in the same environment, pin ours explicitly with `OTEL_PYTHON_DISTRO=vigilon OTEL_PYTHON_CONFIGURATOR=vigilon`.
- A later in-process `register()` call is safely ignored (one warning), so code that also runs outside `opentelemetry-instrument` keeps working.

## Instrumented libraries

`register()` (and the zero-code path) instruments whichever of these are installed — no configuration needed:

- **Web frameworks**: FastAPI, Flask, Django (Django only when `DJANGO_SETTINGS_MODULE` is set — instrumenting without resolvable settings would break apps that configure Django later)
- **HTTP clients**: requests, httpx, urllib3, aiohttp
- **PostgreSQL**: psycopg, psycopg2
- **MySQL**: mysql-connector-python, PyMySQL, mysqlclient
- **Other datastores**: Redis, PyMongo, SQLAlchemy (any driver, including async engines)
- **AWS**: Lambda flush handling (see below)

MySQL through SQLAlchemy is traced at both layers (an engine span with the driver span nested inside); async MySQL drivers (aiomysql, asyncmy) are traced via the SQLAlchemy async engine only.

### App objects created before `register()`

FastAPI and Flask are instrumented by swapping the framework's app class, so an app object built before `register()` ran (usually an import-order problem you can't fix, e.g. inside a framework CLI) is left untouched. The escape hatch is the per-app instrumentor — pass your excluded URLs, since the merged defaults don't apply on this path:

```python
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

FastAPIInstrumentor.instrument_app(app, excluded_urls="/health$,/ready$,/favicon\\.ico$")
```

(Flask: `FlaskInstrumentor().instrument_app(app, excluded_urls=...)`.)

## Pre-fork servers (gunicorn, uWSGI, Celery)

The SDK's exporter runs a background thread that does not survive `fork()`. Call `register()` **once per worker**, in the worker-startup hook — not at master/module import time:

```python
# gunicorn.conf.py
def post_fork(server, worker):
    import vigilon

    vigilon.register(api_key=..., service_name=..., environment=...)
```

```python
# uWSGI (run with --enable-threads)
from uwsgidecorators import postfork


@postfork
def start_vigilon():
    import vigilon

    vigilon.register(api_key=..., service_name=..., environment=...)
```

```python
# Celery worker
from celery.signals import worker_process_init


@worker_process_init.connect
def start_vigilon(**kwargs):
    import vigilon

    vigilon.register(api_key=..., service_name=..., environment=...)
```

## Excluded URLs

Health checks (`/health`, `/ready`, `/favicon.ico`) are excluded from instrumentation by default. Add your own patterns for uncovered health endpoints or long-lived SSE routes that would otherwise dirty service-level metrics:

```python
vigilon.register(
    ...,
    excluded_urls=["/internal/health$", "/events/stream$"],
)
```

Patterns are regexes matched against the full request URL and are merged with the defaults. Excluded routes produce no server span at all — they disappear from tracing and from request metrics, which is exactly what you want for SSE.

## Recording errors

Unhandled exceptions are recorded automatically. For errors your code **catches** and converts into an error response itself — invisible to instrumentation — call `record_error`:

```python
from vigilon import record_error


@app.get("/data")
async def get_data():
    try:
        return await load_data()
    except Exception as error:
        record_error(error)
        return JSONResponse({"message": "failed"}, status_code=500)
```

If there is no active span, `record_error` is a no-op.

## Background jobs

Wrap one execution of a background job — a cron tick, a queue processor, an interval loop — in `with_job_monitor` to report it as a Job Run. Each run starts its own trace, so database queries and outgoing requests made inside the job are attached to that run instead of being orphaned:

```python
from vigilon import with_job_monitor


@with_job_monitor(name="sync-users", schedule="0 3 * * *")
async def sync_users(): ...


# Or as a context manager for inline blocks:
with with_job_monitor(name="refresh-cache"):
    refresh_cache()
```

`name` is a required, stable identifier — never interpolate per-item values (`sync-user-42` fragments one job into unbounded identities). `schedule` is an optional cron expression enabling last-run and stale-job detection. Sync and async functions are both supported; errors are recorded on the run and re-raised.

`with_job_monitor` must run after `register()` has started the SDK. If it runs earlier, the job itself still executes normally, but the run is not reported.

## AWS Lambda

Vigilon detects the Lambda runtime via `AWS_LAMBDA_FUNCTION_NAME` and adds the AWS Lambda instrumentation automatically, which flushes traces at the end of each invocation. No extra configuration is needed.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

## Releasing

Releases go to [PyPI](https://pypi.org/project/vigilon/) from GitHub Actions via Trusted Publishing — no API tokens.

- **Main releases**: every push to `main` runs `release-main.yml`, which bumps the patch version in lockstep (`pyproject.toml` + `uv.lock`), runs the tests, builds the wheel, smoke-tests it in a clean venv against a fake collector (`scripts/smoke_test.py`), publishes it to PyPI, then pushes the `chore(release): X.Y.Z` commit and the `vX.Y.Z` tag. A hand-set, not-yet-tagged version releases as-is instead of being bumped. Reruns are idempotent: a version counts as released once its tag exists, and files already on PyPI are skipped.
- **PR betas**: `pr-beta.yml` builds every PR at a unique PEP 440 pre-release (`X.Y.(Z+1)b<PR>.dev<run>`), runs tests + smoke, and attaches the wheel to the workflow run. Betas never go to PyPI; to try a PR's code, install its git ref: `pip install "vigilon @ git+https://github.com/vigilonio/python@<sha>"`.
- **`vigilon-sdk`** is a reserved, unused name on PyPI (`scripts/pypi-stub/vigilon-sdk`) that points users to `vigilon`.
