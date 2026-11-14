# Shared test vectors and their provenance

Copied, never edited here. Update them where they are made and copy them again.

- `sdk-contract.json` is copied byte for byte from Argus
  `apps/api/sdk-contract.json` at commit
  `78f9621d4673eab0f3e454575b98903352ee02f6` (SHA-256
  `a25284880b58df53e19d2c2854cbe1bc66defb6349f7c9b96009c975ac628a19`). It includes
  `otlp.mcpAttributes`, `otlp.mcpSpans` (a tool whose handler raised is a
  failed call, with its type, ranked before `tool_error`),
  `otlp.limits.mcpPayloadBytes` and `exportBatchBytes`, the standard MCP naming
  attributes, `tool_error`, and the `sdk-mcp-*` fixtures. It is written by Argus's
  `apps/api/test/sdk-contract.test.ts`.
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
