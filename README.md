# Agentrail for Python

[![CI](https://github.com/agentrail-co/agentrail-python/actions/workflows/ci.yml/badge.svg)](https://github.com/agentrail-co/agentrail-python/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agentrail-python)](https://pypi.org/project/agentrail-python/)
[![Python](https://img.shields.io/pypi/pyversions/agentrail-python)](https://pypi.org/project/agentrail-python/)

`agentrail-python` records what happens inside your Python backend when an AI
agent (or a person) uses your product, so [Agentrail](https://www.agentrail.co)
can link it into that visitor's journey. It records:

- each incoming HTTP request and each outgoing call;
- who the request was for, once your application has authenticated them;
- the business steps you name (`checkout.confirm`, `delivery.requested`);
- structured logs, tied to the request that wrote them;
- MCP tool, resource and prompt calls.

It sends OpenTelemetry (OTLP over HTTP/protobuf) to the Agentrail collector.
By default it sends no request or response bodies, no arguments, no query
values and no raw identities.

Works with FastAPI, Starlette, Django, Flask and any other ASGI or WSGI
application. Requires **Python 3.10 or later**.

- [Install](#install)
- [Quickstart](#quickstart): [FastAPI](#fastapi), [Starlette and other ASGI](#starlette-and-other-asgi-apps),
  [Django](#django), [Flask and other WSGI](#flask-and-other-wsgi-apps)
- [Outgoing calls](#outgoing-calls)
- [Identity and milestones](#identity-and-milestones)
- [Workflow and plugin evidence](#workflow-and-plugin-evidence)
- [Logs](#logs)
- [MCP servers](#mcp-servers)
- [An existing OpenTelemetry setup](#an-existing-opentelemetry-setup)
- [Approximate IP location](#approximate-ip-location)
- [Your own servers](#your-own-servers)
- [Delivery, shutdown and diagnostics](#delivery-shutdown-and-diagnostics)
- [What is sent, and what is not](#what-is-sent-and-what-is-not)
- [Node, Go and Python](#node-go-and-python)
- [Configuration reference](#configuration-reference)

## Install

```sh
pip install agentrail-python
# with the integrations you use:
pip install "agentrail-python[httpx,requests,mcp]"
```

Then, in Agentrail, open your workspace's journey setup and create a backend
source. In the setup screen this source type is called **Node**; it is the
backend source for every backend SDK, Python included. You need two values,
and both stay on your server, never in a page:

- the **source secret**, which authenticates what your backend sends;
- the workspace **hash key**, which hashes identities before they leave your
  process and signs trace context between your services.

## Quickstart

Create the client once, when the process starts, before it serves requests:

```python
# agentrail_setup.py
import os

import agentrail_python as ar

agentrail = ar.init(
    ar.Options(
        collector=ar.CollectorOptions(
            endpoint="https://api.agentrail.co/ingest/otlp",
            secret=os.environ["AGENTRAIL_SOURCE_SECRET"],
        ),
        hash_key=os.environ["AGENTRAIL_HASH_KEY"],
        service_name="orders-api",
    )
)
```

`init` installs Agentrail's OpenTelemetry tracer provider, logger provider and
propagator for the process. If your process already has an OpenTelemetry
provider (Sentry, Datadog, your own), `init` refuses to replace it: use
[`create_processors`](#an-existing-opentelemetry-setup) instead. Calling `init`
again with the same `Options` object returns the same client.

The SDK reads no environment variables itself. Read and check your own
configuration before calling `init`; a missing value is a configuration error,
not a default.

### FastAPI

```python
from fastapi import Depends, FastAPI

import agentrail_python as ar
from agentrail_python.asgi import AgentrailASGIMiddleware
from agentrail_setup import agentrail
from my_app.auth import current_user  # your own authentication

app = FastAPI()
app.add_middleware(AgentrailASGIMiddleware, client=agentrail)


@app.post("/orders/{order_id}/confirm")
async def confirm(order_id: str, user=Depends(current_user)):
    agentrail.identify(ar.VerifiedIdentity(kind="user", id=user.id))
    ...
```

The route template (`/orders/{order_id}/confirm`) is recorded with each request.

### Starlette and other ASGI apps

```python
app = agentrail.asgi(app)  # Starlette, Quart, Litestar, any ASGI 3 application
```

### Django

```python
# asgi.py
from django.core.asgi import get_asgi_application
from agentrail_setup import agentrail

application = agentrail.asgi(get_asgi_application())

# or wsgi.py
from django.core.wsgi import get_wsgi_application
application = agentrail.wsgi(get_wsgi_application())
```

### Flask and other WSGI apps

```python
from flask import Flask
from agentrail_setup import agentrail

app = Flask(__name__)
app.wsgi_app = agentrail.wsgi(app.wsgi_app)
```

Install the middleware once, around the part of your application you want
recorded. If you install it after your authentication middleware, the
authentication step itself is not recorded. Each request becomes one entry
step; work it does (database calls, other OpenTelemetry spans) is recorded
beneath it.

**Trace context from callers.** A request carrying a valid signed handoff
from your own Agentrail edge or another backend SDK continues that trace. The
signature covers the method, the exact path and query as sent, the workspace
key ID, the trace parent and an issue time that is valid for 60 seconds, so
keep your servers' clocks in sync. A request without one (an agent, a browser,
anyone on the internet) starts a new trace; a `traceparent` it sends is kept
only as a link, never as the parent.

For WSGI, the exact target comes from `RAW_URI` or `REQUEST_URI`, which
gunicorn, uWSGI and Werkzeug set. Under a server that does not, the target is
rebuilt from the decoded path, and a signed handoff to a path with
percent-encoding may not verify.

## Outgoing calls

Wrap the HTTP clients your handlers use, so their calls are recorded and,
where you allow it, carry signed trace context to your own services:

```python
import httpx
import requests
from agentrail_python.httpx import async_transport, transport
from agentrail_python.requests import adapter

client = httpx.Client(transport=transport(agentrail))
async_client = httpx.AsyncClient(transport=async_transport(agentrail))

session = requests.Session()
session.mount("https://", adapter(agentrail))
session.mount("http://", adapter(agentrail))
```

Signed context goes only to the exact origins you list:

```python
ar.Options(..., outbound_origins=["https://inventory.internal", "http://billing:8080"])
```

An origin is a lowercase scheme and host with a non-default port, nothing
else. The destination a call actually connects to decides, not a `Host`
header. Each redirect is checked again, so a redirect from an allowed origin
to another one is not signed. Managed calls never forward `traceparent`,
`tracestate`, `baggage`, `sentry-trace` or a handoff header they did not sign
themselves. Calls made outside a request or milestone pass through untouched.

`transport(agentrail, base)` wraps your own `httpx.HTTPTransport` (for proxies,
retries or certificates); `adapter(agentrail, **kwargs)` takes the same
arguments as `requests.adapters.HTTPAdapter`. A `requests` call's step ends
when `send` returns: with `stream=True` it does not wait for the body.

## Identity and milestones

```python
# Only after your application has authenticated this caller:
agentrail.identify(
    ar.VerifiedIdentity(
        kind="agent_registration",  # user, agent_registration, agent_instance, oauth_client, api_key
        id=authenticated_agent.id,
        acting_user_id=authenticated_agent.user_id,  # optional
    )
)
```

IDs are hashed with the workspace hash key before they leave the process.
Nothing a caller merely claims (a header, a user agent, MCP client details)
is ever treated as identity. `identify` works inside a request or milestone;
outside one it reports `identity_outside_request` and records nothing. The
first identity in a request stands: repeating it does nothing, and a
different one is recorded as its own `auth` step.

Name the business steps that matter:

```python
step = ar.MilestoneContext(surface="custom", kind="checkout.confirm")

agentrail.milestone("checkout.confirm", step)  # an instant step

with agentrail.start_milestone("checkout.confirm", step):  # or `async with`
    confirm_order()

@agentrail.with_milestone("checkout.confirm", step)  # each call is one step
async def confirm_order(): ...
```

A milestone's exception is recorded (its class name only) and raised
unchanged; its return value is untouched. Names and kinds are stable labels of
1 to 256 characters, never IDs or arguments. Outside a request, a milestone
starts its own trace; an unrelated span that was current becomes a link.

The current request or milestone follows your code through `await`, asyncio
tasks and `asyncio.to_thread`. A thread you start yourself does not inherit
it; run its work with `contextvars.copy_context().run(...)`.

## Workflow and plugin evidence

Once your application has authorized a business operation, pass its operation
reference to the milestone. Use a new reference for each attempt, and the same
one for its start, progress, plugin calls and outcome, across services that
share the workspace hash key and namespace:

```python
agentrail.milestone(
    "delivery.requested",
    ar.MilestoneContext(
        surface="custom",
        kind="business.event",
        performance=ar.PerformanceContext(
            environment="production",  # or "test"
            operation=ar.AuthorizedOperation(
                namespace_id=OPERATION_NAMESPACE_ID,
                instance_ref=delivery_attempt.id,
                role="start",  # start, progress or terminal
            ),
            event_name="delivery.requested",
            plugin=ar.PluginInvocation(binding_id=PLUGIN_BINDING_ID, invocation_id=str(uuid.uuid4())),
        ),
    ),
)
```

Record a `terminal` milestone with your configured success event only after
your backend has confirmed the outcome. A tool's result or an HTTP 200 is not
business completion, and the MCP integration never declares one.

The operation reference never leaves your process: the SDK sends
HMAC-SHA256 under the workspace hash key of the JSON array
`["agentrail.operation", namespace_id, instance_ref]`, the same as Node and Go,
so steps from all three join. Supply `operation` with `event_name`, `operation`
with `plugin`, or `plugin` alone (plugin activity with no workflow credit).
Namespace and binding IDs come from Agentrail. Invalid evidence is left off the
step and reported as `invalid_performance`; the milestone and your code still
run. Evidence applies only to that milestone, never to its child spans.

## Logs

```python
import logging
from agentrail_python.logging import AgentrailHandler

logging.getLogger().addHandler(AgentrailHandler(agentrail))

log = logging.getLogger("orders")
log.error("payment declined", extra={"event.name": "checkout.failed"})
```

By default a log sends its severity, event name, the exception's class name
(`error.type`) and the exact request and span it was written in. Message text
and other fields are sent only when you name them:

```python
ar.Options(..., logs=ar.LogsOptions(include_fields=["message", "order_status"]))
```

Only the named top-level fields with scalar values (text, numbers, booleans,
null) are sent, in the order named, as a JSON object of at most 4,096
characters; a long value is shortened to fit, and a field that cannot fit is
left out. `message` is the formatted message. Never name fields holding
credentials, identities or customer content. Set
`LogsOptions(exclude_uncorrelated=True)` to send only logs written inside a
request or milestone.

You can also record a log directly, or add `processors.log_processor` to your
own OpenTelemetry `LoggerProvider`:

```python
agentrail.log(ar.StructuredLog(severity_number=17, error_type="PaymentDeclined", event_name="checkout.failed"))
```

Logs never prove anything about a journey on their own: they describe the step
they belong to.

## MCP servers

For servers built with the official [`mcp`](https://pypi.org/project/mcp/)
SDK (2.x, `MCPServer`):

```python
from mcp.server.mcpserver import MCPServer
from agentrail_python.mcp import mcp_capture

capture = mcp_capture(agentrail)
server = MCPServer("orders", middleware=[capture.middleware])


def checkout(item: str) -> str: ...


server.tool(name="checkout")(capture.tool("checkout", checkout))
server.resource("catalog://items", name="catalog")(capture.resource("catalog", read_catalog))
server.prompt(name="greet")(capture.prompt("greet", greet))
```

Each call becomes an MCP step named `mcp.tools/call:checkout`, nested under
the HTTP request when the server runs behind the ASGI middleware
(`agentrail.asgi(server.streamable_http_app(...))`), or its own trace
otherwise. The middleware adds the calling client's name and version and the
protocol version; that is what the client says about itself, never an
identity. A result with `isError` marks the step failed and is returned
unchanged. Pass the name you registered, never a requested resource URI.
Arguments, results, prompt text and resource URIs are never recorded.

The MCP SDK opens its own OpenTelemetry span for every message. Inside a
recorded request it is kept as supporting work, not as a second entry.

## An existing OpenTelemetry setup

If your process already owns an OpenTelemetry tracer provider, build
Agentrail's pieces into it when you construct it. A running provider cannot
recover spans its sampler already dropped.

```python
from opentelemetry import propagate, trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

processors = ar.create_processors(
    ar.Options(..., propagator=propagate.get_global_textmap())
)
provider = TracerProvider(
    sampler=processors.sampler(ParentBased(TraceIdRatioBased(0.1))),
    resource=your_resource,
)
provider.add_span_processor(your_processor)
provider.add_span_processor(processors.span_processor)
trace.set_tracer_provider(provider)
propagate.set_global_textmap(processors.propagator)
your_logger_provider.add_log_record_processor(processors.log_processor)

agentrail = processors.bind(provider)  # once
```

`create_processors` registers nothing globally, and Agentrail never changes
what your other processors and exporters receive. The sampler wrapper keeps
spans inside a recorded request that your sampler would drop as
**record-only**, with your sampled flag unchanged: exporters that send only
sampled spans behave as before. Pass your existing propagator as
`propagator` and register the returned `processors.propagator`: it hands
unmanaged work to yours, and keeps unsigned headers out of recorded requests.
Use Agentrail's ASGI or WSGI middleware for incoming requests instead of
another incoming HTTP instrumentation, so each request has one entry.
`agentrail.shutdown()` never shuts down your provider.

## Approximate IP location

```python
def location(request: ar.IncomingRequest) -> ar.IPLocation | None:
    country = request.headers.get("cf-ipcountry")  # trust it only behind Cloudflare
    if not country or not country.isalpha() or country == "XX":
        return None
    return ar.IPLocation(country=country, source="cloudflare")

ar.Options(..., incoming_location=location)
```

The hook runs once per recorded request, before your handler, and must return
quickly: read trusted platform headers or a local database loaded at startup,
never call a network service. Resolve the client address with your own proxy
configuration and never trust an arbitrary forwarded header. `country` is a
two-letter uppercase code; `region` and `city` are shortened to 128
characters; `source` is `cloudflare`, `vercel`, `dbip` or `source` (another
trusted provider). Return `None` when there is no location. A hook that raises
or returns an invalid location is reported as `location_failed`, and the
request carries on without one. The location is sent on the request's entry
and its milestones, never the address itself. An approximate location
describes a network exit, which may be a VPN, a proxy or an agent's host, not
the person.

## Your own servers

Calls between your own services are not visitors or agents. Leave them out
with `ignore_incoming_request`, and check something only your servers can
send, such as a shared secret: a user agent or an address is what any caller
can claim.

```python
import hmac
import os

internal_secret = os.environ["INTERNAL_SECRET"]
if not internal_secret:
    raise RuntimeError("INTERNAL_SECRET is empty")

ar.Options(
    ...,
    ignore_incoming_request=lambda request: hmac.compare_digest(
        request.headers.get("x-my-internal", ""), internal_secret
    ),
)
```

Check the secret is set before starting: if it were empty, every request
without the header would match and nothing would be recorded. Use the same
hook for health checks. If the collector itself runs in this process, exclude
its ingestion routes here too.

## Delivery, shutdown and diagnostics

Records wait in a bounded in-memory queue per signal (traces, logs) and are
sent by one background thread each:

| Setting (`QueueOptions`) | Default |
| --- | --- |
| `max_records` (including the batch being sent) | 2048 |
| `batch_size` | 128 |
| `scheduled_delay` | 1 s |
| `export_timeout` | 5 s |
| `flush_timeout`, `shutdown_timeout` | 10 s |

One batch is sent at a time. The OTLP exporter retries retryable answers
within `export_timeout`; a refusal, or a batch still failing at the deadline,
is dropped and reported. Nothing is written to disk.

```python
agentrail.flush()      # waits for attempts on what was recorded so far
agentrail.shutdown()   # drains once and stops; terminal and idempotent
agentrail.diagnostics()  # accepted, exported, dropped, pending per signal
```

Stop accepting requests and let them finish, then call `shutdown()`
(`await asyncio.to_thread(agentrail.shutdown)` from async code). With `init`,
the providers also drain when the interpreter exits. A finished `flush` means
the attempts ended; check `diagnostics()` and `on_error` for anything lost.

`on_error` receives a `CaptureError` with a `code`, `signal` and `count`, and
never any customer data:

| Code | Meaning |
| --- | --- |
| `queue_full`, `closed` | A record was refused: the queue was full, or after `shutdown` |
| `export_failed`, `deadline` | A batch was refused or failed, or a flush or shutdown deadline passed |
| `handoff_refused` | A signed handoff did not verify (or one could not be signed) |
| `entry_not_recording` | A sampler dropped an entry that should have been recorded |
| `location_failed` | The location hook raised or returned an invalid location |
| `identity_outside_request` | `identify` was called outside a request or milestone |
| `invalid_performance` | Workflow or plugin evidence was invalid and left off |

Without `on_error`, these go to standard error. The callback runs without
capture, so logging from it is never recorded. Capture problems never change
your application's response. Invalid options, identities and milestone labels
are programming errors and raise `ConfigurationError`.

## What is sent, and what is not

Sent, from an allowlist:

- for each request and outgoing call: the method, the URL without its query
  (except valid Agentrail `h` markers), the route template, the status code,
  the user agent and the error class name;
- for supporting spans: only their place in the trace, their timing, status
  and error class name; their names become `backend.operation`;
- hashed identities, milestone names and kinds, workflow and plugin evidence,
  MCP client details;
- the resource: service name, instance ID, SDK name and version, and the hash
  key's ID (never the key).

Never sent: request or response bodies, query values, headers, cookies,
arguments, results, exception messages, span events, other span attributes,
trace state, baggage, raw identities, raw operation references, prompt text or
resource URIs. Log text only as described in [Logs](#logs). Your service
names, routes, milestone names and the fields you select are yours to keep
free of sensitive data.

## Node, Go and Python

The three backend SDKs send the same events and share their signing, hashing
and marker rules, checked against the same test vectors, so services in any
mix of the three link into one journey.

| | Node (`agentrail-node`) | Go (`agentrail-go`) | Python (`agentrail-python`) |
| --- | --- | --- | --- |
| Incoming HTTP | Node HTTP instrumentation | `Middleware` | ASGI and WSGI middleware |
| Outgoing HTTP | `http`, `fetch` | `Transport` | `httpx`, `requests` |
| Signed handoffs, allowed origins | Yes | Yes | Yes |
| Verified identity | `identify` | `Identify` | `identify` |
| Milestones | `milestone` | `Milestone`, `StartMilestone`, `WithMilestone` | `milestone`, `start_milestone`, `with_milestone` |
| Workflow and plugin evidence | Yes | Yes | Yes |
| Logs | Pino, OpenTelemetry | `slog`, OpenTelemetry | `logging`, OpenTelemetry |
| MCP | `@modelcontextprotocol/server` | official Go SDK | official `mcp` SDK |
| Existing OpenTelemetry | `createProcessors` | `NewProcessors` + `Bind` | `create_processors` + `bind` |
| Standalone startup | `init` (installs globals) | `New` (no globals) | `init` (installs globals) |
| IP location, ignored requests | Yes | Yes | Yes |
| Queue defaults | 2048 / 128 / 1 s / 5 s / 10 s | same | same |

For the edge and the page itself, use the JavaScript packages
[`agentrail-edge` and `agentrail-browser`](https://github.com/agentrail-co/agentrail-js).

## Configuration reference

`Options` (all keyword arguments):

| Option | |
| --- | --- |
| `collector` | `CollectorOptions(endpoint=..., secret=...)`: the OTLP base URL (`/v1/traces` and `/v1/logs` are added) and the backend source secret |
| `hash_key` | The workspace hash key |
| `service_name` | A stable, non-sensitive service name |
| `service_instance_id` | Optional; a random ID per process by default |
| `outbound_origins` | Exact origins that receive signed trace context; none by default |
| `ignore_incoming_request` | `(IncomingRequest) -> bool`; True leaves the request unrecorded |
| `incoming_location` | `(IncomingRequest) -> IPLocation \| None` |
| `logs` | `LogsOptions(include_fields=..., exclude_uncorrelated=...)` |
| `queue` | `QueueOptions(...)`, see above |
| `propagator` | Your existing propagator, for unmanaged work |
| `on_error` | `(CaptureError) -> None` |

`IncomingRequest` has `method`, `target` (path and query as sent), `scheme`,
`host`, `headers` (lowercase names), `client` (the peer address the server
saw) and `raw` (the ASGI scope or WSGI environ).

Before 1.0, a minor version (0.1 to 0.2) may change the API.

## Development

```sh
uv sync
uv run ruff format --check src tests integration scripts
uv run ruff check src tests integration scripts
uv run pyright
uv run pytest
scripts/check-consumer.sh                       # the built wheel, FastAPI and Flask, in a fresh venv
ARGUS_DIR=/path/to/argus integration/run.sh     # the real collector, with Docker
```

The receiver gate needs a checkout of Agentrail's collector with its
dependencies installed and Docker running; see
[integration/README.md](integration/README.md). Shared test vectors and their
provenance are in [testdata/](testdata/README.md).

## Releasing

A `v<version>` tag publishes the package to PyPI
(`.github/workflows/release.yml`). The tag must match `VERSION` in
`src/agentrail_python/_version.py`. Only the repository's admins can push a
`v*` tag, and the publish job waits for approval in the `pypi` environment.
PyPI trusts the workflow (trusted publishing), so no token is stored, and each
file gets a signed attestation. A run that uploaded some files and then failed
can be run again: it skips what PyPI already has.

## Licence

[Apache-2.0](LICENSE).
