<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vigilonio/python/main/.github/assets/sigil-thin-dark-120.png" />
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/vigilonio/python/main/.github/assets/sigil-thin-dark-solid-120.png" />
    <img src="https://raw.githubusercontent.com/vigilonio/python/main/.github/assets/sigil-thin-dark-solid-120.png" alt="Vigilon" width="100" />
  </picture>
</p>

<p align="center">
  <a href="https://github.com/vigilonio/python/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-ISC-blue.svg" alt="License: ISC"></a>
  <a href="https://pypi.org/project/vigilon/"><img src="https://img.shields.io/pypi/v/vigilon.svg" alt="PyPI"></a>
  <a href="https://pypi.org/project/vigilon/"><img src="https://img.shields.io/pypi/pyversions/vigilon.svg" alt="Python versions"></a>
</p>

# Vigilon Python SDK

Official Python SDK for [Vigilon](https://vigilon.io).

Vigilon is application monitoring for SaaS applications, based on OpenTelemetry. Out of the box it provides RESTful service and endpoint health, error monitoring and alerting, end-to-end tracing, and background job monitoring.

The SDK sets up standard [OpenTelemetry](https://opentelemetry.io/) instrumentation for your app and exports it to Vigilon over OTLP, with no proprietary agent. One `register()` call, or the zero-code `opentelemetry-instrument` launcher, instruments FastAPI, Flask and Django, plus common HTTP and database clients.

## Install

```bash
pip install vigilon
```

Requires Python 3.10 or later.

## Quick start

Create an API key in the [Vigilon dashboard](https://app.vigilon.io) under your project's **Settings** page, **API Keys** tab.

Then call `register()` as the **first statement of your entrypoint**, before application modules import the frameworks being instrumented:

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

Full product documentation is at [vigilon.io/docs](https://vigilon.io/docs).

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

## Support

- Bugs and feature requests: [GitHub Issues](https://github.com/vigilonio/python/issues)
- Security vulnerabilities: report them privately to [security@vigilon.io](mailto:security@vigilon.io) instead of opening a public issue.

## Contributing

Build, test, and release instructions are in [CONTRIBUTING.md](https://github.com/vigilonio/python/blob/main/CONTRIBUTING.md).

## License

[ISC](https://github.com/vigilonio/python/blob/main/LICENSE)
