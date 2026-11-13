# Shared test vectors and their provenance

Copied, never edited here. Update them where they are made and copy them again.

- `sdk-contract.json` is copied byte for byte from Argus
  `apps/api/sdk-contract.json` at commit
  `ddb046d7fb5a884724e0f62fdf4441c64dee452c` (SHA-256
  `6a0b1e13d364da92831ba495b30a42ae0c1da7119c771e4b4a11a2e93743f498`). It includes
  `otlp.mcpAttributes`, `otlp.mcpSpans`, `otlp.limits.mcpPayloadBytes` and
  `exportBatchBytes`, the standard MCP naming attributes, `tool_error`, and
  the `sdk-mcp-*` fixtures. It is written by Argus's
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
