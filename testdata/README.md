# Shared test vectors and their provenance

Copied, never edited here. Update them where they are made and copy them again.

- `sdk-contract.json` is copied byte for byte from Argus
  `apps/api/sdk-contract.json` at commit
  `9625ffe6e2b60cd539a8fbd18445273d437c93fd` (SHA-256
  `20adfff8a062274d104e9251e36834c3ef1b92821b03fc0a5d44ad86070afcba`). It includes
  `otlp.mcpAttributes`, `otlp.mcpSpans` (a tool whose handler raised is a
  failed call, with its type, ranked before `tool_error`; `requestFacts`, what
  a step says of the HTTP request that carried its message),
  `otlp.mcpRequestFacts` (two recorded requests, Claude Code 2.1.318 and Codex
  0.166.0, and the attributes an SDK sends for each), `otlp.headerNames`,
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
