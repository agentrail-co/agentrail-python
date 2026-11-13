# Shared test vectors and their provenance

Copied, never edited here. Update them where they are made and copy them again.

- `sdk-contract.json` is copied byte for byte from Argus
  `apps/api/sdk-contract.json` as generated on branch `feat/mcp-analytics`
  from base `23eaa10c2e85a5b86648468bc5e28e304394c19e`, before that change was
  committed (SHA-256
  `3bd2c8048b01bafbc1d08dc41a49646b481c71f8bacbaad3a866c070d323f4ef`). It adds
  `otlp.mcpAttributes`, `otlp.mcpSpans`, `otlp.limits.mcpPayloadBytes` and
  `exportBatchBytes`, and the `sdk-mcp-*` fixtures. It is
  written by Argus's `apps/api/test/sdk-contract.test.ts`. Copy it again from
  the commit that lands it.
- `interoperability/contract.json`, `javascript.json` and `markers.json` are
  copied from agentrail-go `testdata/interoperability/` at `ad62f2b34c71143de38388236cff431013d1227e`. Its
  `generate.mjs` writes them with `node:crypto`, native `JSON.stringify` and
  agentrail-core's marker parser, independently of any SDK's own encoder.
- `interoperability/performance.json` is written here by
  `node testdata/interoperability/generate-performance.mjs`, with `node:crypto`
  and `JSON.stringify`, as agentrail-node's `performance.ts` computes the
  operation hash.

The receiver recognizes `agentrail-python` beside `agentrail-node` and
`agentrail-go` for identity and milestones; an older receiver accepts the spans
but drops those. `integration/run.sh` proves it against a real receiver.
