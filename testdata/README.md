# Shared test vectors and their provenance

Copied, never edited here. Update them where they are made and copy them again.

- `sdk-contract.json` is copied byte for byte from Argus
  `apps/api/sdk-contract.json` at commit `a5092a616`, branch
  `feat/cli-journeys` (agentrail-co/argus, the CLI journeys change; SHA-256
  `9d46a125cd143df327f02830db604a96a89874ee3b0eb763445262cfa662a25a`). It includes `cliHeader` (the `x-agentrail-cli` header a customer's
  CLI sends, how it is read, and its vectors), `otlp.cliAttributes` and
  `otlp.cliSpans`, which the middleware sends, and the event's `cli`; and
  `otlp.mcpAttributes`, `otlp.mcpSpans` (a tool whose handler raised is a
  failed call, with its type, ranked before `tool_error`; `requestFacts`, what
  a step says of the HTTP request that carried its message),
  `otlp.mcpRequestFacts` (two recorded requests, Claude Code 2.1.318 and Codex
  0.166.0, and the attributes an SDK sends for each), `otlp.headerNames` (with
  `cdn-loop` and `true-client-ip` among the names no caller writes), the client
  id a connected app's token was issued to (`principal.client`),
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
