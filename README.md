# Agentrail for Python

[![CI](https://github.com/agentrail-co/agentrail-python/actions/workflows/ci.yml/badge.svg)](https://github.com/agentrail-co/agentrail-python/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agentrail-python)](https://pypi.org/project/agentrail-python/)
[![Python](https://img.shields.io/pypi/pyversions/agentrail-python)](https://pypi.org/project/agentrail-python/)
[![Licence](https://img.shields.io/pypi/l/agentrail-python)](https://github.com/agentrail-co/agentrail-python/blob/main/LICENSE)

`agentrail-python` adds your Python servers to the journeys
[Agentrail](https://www.agentrail.co) follows: the requests they answer, the
calls they make, the identity your application verified, the milestones you
name, your logs and your MCP traffic. Bodies and raw identities never leave
your process; MCP arguments and results leave it only when you turn on
`capture_payloads`.

Works with FastAPI, Starlette, Django, Flask and any ASGI or WSGI application.
Requires **Python 3.10 or later**.

## Install

```sh
pip install agentrail-python
```

Add `[httpx]`, `[requests]` or `[mcp]` for those integrations, for example
`pip install "agentrail-python[httpx,mcp]"`.

In Agentrail, open **Journeys**, select **Set up** and choose **Your servers**.
Agentrail shows the source's **Secret** and the workspace's **Hash key** once,
with your collector address. Keep both on your servers, never in a page, for
example as `AGENTRAIL_SECRET` and `AGENTRAIL_HASH_KEY`.

## Quick start

```python
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

import agentrail_python as ar
from agentrail_python.asgi import AgentrailASGIMiddleware
from my_app.auth import current_user  # your own authentication

# Once, when the process starts.
agentrail = ar.init(
    ar.Options(
        collector=ar.CollectorOptions(
            endpoint="https://<collector>/ingest/otlp",
            secret=os.environ["AGENTRAIL_SECRET"],
        ),
        hash_key=os.environ["AGENTRAIL_HASH_KEY"],
        service_name="orders-api",
    )
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    agentrail.shutdown()  # sends what is still queued


app = FastAPI(lifespan=lifespan)
app.add_middleware(AgentrailASGIMiddleware, client=agentrail)  # records each request


@app.post("/orders/{order_id}/confirm")
async def confirm(order_id: str, user=Depends(current_user)):
    # Only after your application has authenticated the caller. Ids are hashed.
    agentrail.identify(ar.VerifiedIdentity(kind="user", id=user.id))

    # A business step, with a stable name.
    with agentrail.start_milestone(
        "checkout.confirm", ar.MilestoneContext(surface="custom", kind="checkout.confirm")
    ):
        ...
```

Other frameworks wrap the application once:

```python
application = agentrail.asgi(get_asgi_application())  # Django (ASGI), Starlette, any ASGI app
application = agentrail.wsgi(get_wsgi_application())  # Django (WSGI)
app.wsgi_app = agentrail.wsgi(app.wsgi_app)            # Flask, any WSGI app
```

### MCP

With the official `mcp` SDK, add the middleware first. Each JSON-RPC message
your server receives becomes a step: its method, outcome and JSON-RPC error
code, message sizes, the calling client and, on stateful streamable HTTP, a
keyed hash of the session ID (the opening `initialize` included). Wrapped
tools, resources and prompts are child spans of that step. Step names carry
the tool, the prompt and the requested resource URI (without its userinfo,
query or fragment), with payload capture off too.

A wrapped tool that raises is a failed call: `MCPServer` answers the exception
with an `isError` result of its own, and the step keeps `isError` but carries
the exception's type. A result your tool returns with `isError` is a failed
result (`tool_error`).

```python
from mcp.server.mcpserver import MCPServer
from agentrail_python.mcp import mcp_capture

capture = mcp_capture(agentrail)
server = MCPServer("orders", middleware=[capture.middleware])  # first in the list
server.tool(name="checkout")(capture.tool("checkout", checkout))

# Streamable HTTP: wrap the app so each MCP step sits under its HTTP request.
app = agentrail.asgi(server.streamable_http_app())
```

Arguments and results are off by default. To send them, pass a redact hook; it
gets copies of `method`, `name`, `request` and `response` and returns what to
send, or `None` to send nothing:

```python
def redact(payload: ar.McpPayload):
    request = dict(payload["request"] or {})
    request.pop("arguments", None)
    return {"request": request, "response": payload["response"]}


capture = mcp_capture(agentrail, capture_payloads=ar.CapturePayloads(redact=redact))
```

When a span's two payloads together pass 14 MiB, they are cut to fit on a
character boundary, the longer first (both, when each passes half), and each
cut side is flagged. A multi-round-trip `requestState` is never sent. A request the MCP SDK refuses before its
middleware runs (a malformed body, a wrong header, an unknown session) is no
MCP step; behind `agentrail.asgi` it is still recorded as its HTTP request.

Agentrail never fails your request: anything it could not capture or send goes
to your `on_error` callback.

## Documentation

| | |
| --- | --- |
| [Install the Python SDK](https://www.agentrail.co/docs/guides/journeys-python) | Setup for each framework, outgoing calls, identity, milestones, logs, MCP and location |
| [Python SDK reference](https://www.agentrail.co/docs/guides/journeys-python-reference) | Every option, an existing OpenTelemetry setup, delivery and error codes, framework notes |
| [Leave out your own servers](https://www.agentrail.co/docs/guides/journeys-own-servers) | Skip calls between your own services |
| [Plugin workflow tracking](https://www.agentrail.co/docs/guides/plugin-performance) | Report workflow attempts and plugin calls on milestones |
| [What Agentrail records](https://www.agentrail.co/docs/guides/journeys-data) | What is sent, what is hashed, and what never leaves your process |

The same SDK is available for [Node](https://github.com/agentrail-co/agentrail-js/tree/main/packages/node)
(`agentrail-node`) and [Go](https://github.com/agentrail-co/agentrail-go) (`agentrail-go`).

## Contributing

Issues and pull requests are welcome. See
[CONTRIBUTING.md](https://github.com/agentrail-co/agentrail-python/blob/main/CONTRIBUTING.md)
for development and releasing.

## Licence

[Apache-2.0](https://github.com/agentrail-co/agentrail-python/blob/main/LICENSE).
