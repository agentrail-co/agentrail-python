# Agentrail Python SDK

Public Python package `agentrail-python` (import `agentrail_python`) for backend
journey evidence. Reference behaviour lives in `agentrail-co/agentrail-js`
`packages/node`; `agentrail-co/agentrail-go` is the closest structural port. The
collector schema belongs to Argus.

- Read the implementation and README before changing behaviour.
- Run `uv run ruff format --check src tests integration scripts`,
  `uv run ruff check src tests integration scripts`, `uv run pyright`,
  `uv run pytest` and `scripts/check-consumer.sh` before reporting completion.
  Run `integration/run.sh` against an Argus checkout for any wire change.
- Wire output stays byte-compatible with Node and Go: attribute names and
  values, hash formats, the handoff payload and the operation hash. The vectors
  in `testdata/` are copied, never edited here (`testdata/README.md`).
- A test must earn its place: finish "this test fails if someone ..." with a
  realistic change, and watch it fail by making that change. Run mutations with
  `PYTHONDONTWRITEBYTECODE=1`: a restored file of the same size within the same
  second reuses the mutated bytecode.
- Never retain unsanitized spans or logs in delivery queues or change customer
  exporter input. Never export bodies, arguments, results or raw identity by
  default.
- Managed requests continue only verified signed handoffs. The actual
  destination, exact method and encoded target decide outbound admission.
- `create_processors` registers no globals; only `init` installs providers, and
  it refuses to replace an existing one. Customer-owned providers stay
  customer-owned.
- Capacity includes the batch in flight. Preserve the application's exceptions,
  results and streaming responses. Capture problems go to `on_error`, never to
  the application's response.
- Never hide a failure: handle only the errors a line can raise, by name. The
  broad catches that exist are for application callbacks and the export
  thread, and each says why.
- No production credentials or customer evidence in this public repository.
- Feature work uses a branch; no rebase, force push or amend of pushed commits.
  Merge pull requests with a merge commit.
- Release tags must match `VERSION` in `src/agentrail_python/_version.py`.
