# Real Argus receiver gate

Run from this checkout with uv, Docker, and an Argus checkout with its pinned
Node 24 and pnpm dependencies installed:

```sh
ARGUS_DIR=/absolute/path/to/argus ./integration/run.sh
```

`capture.py` uses the public package: a Starlette app behind the ASGI
middleware identifies a synthetic user, opens a custom milestone, writes two
correlated logs and makes an outgoing `httpx` call beneath a supporting
OpenTelemetry span, with a synthetic location hook. A local collector keeps the
exact OTLP protobuf bytes. There are no provider calls, real identities or
production credentials; the collector token and hash key are synthetic.

The runner copies `argus-receiver.integration.test.ts` into a uniquely named
temporary Argus API test, then runs `pnpm --filter @argus/api test:file`.
Argus's Testcontainers fixture makes a real PostgreSQL server and a migrated
database. The test creates a backend (`node`) journey source through the public
API, sends the captured bytes logs first and trace batches reversed, then
repeats every delivery unchanged. Fresh reads check that:

- four spans and two logs are stored once, through the retries;
- the resource keeps `agentrail-python`, its service identity and hash key ID;
- the HTTP entry and the milestone are both entries, with supporting spans
  beneath the milestone;
- principal and acting-user hashes equal independently computed HMACs, and the
  location is on both entries;
- the linker stores one journey with an active trace proof, the milestone's
  exact supporting subtree, both logs attached, one error, and no diagnostics.

The copied test and the capture are removed on exit, failures included. The
test is the Go SDK's gate with the component, names and keys changed.

## Evidence

Against Argus `847475c903e803f1b52aa2f00e16eb7d6d6dec68`
(`feat/python-sdk-recognition`, which adds `agentrail-python` to the receiver's
backend SDKs), the gate passed: one test file, one test. With that one line
removed (the receiver as on `main` before it), the gate failed: "Python server
entry and explicit milestone must both be admitted", because only the HTTP
entry was admitted. Deploy the receiver change before customers install the
package.
