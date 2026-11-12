# Shared test vectors and their provenance

Copied, never edited here. Update them where they are made and copy them again.

- `sdk-contract.json` is copied byte for byte from Argus
  `apps/api/sdk-contract.json` at revision `2074d586322c0d6097a840b392aa75e846f9eeb8` (identical to the
  copy in agentrail-go). It is written by Argus's `apps/api/test/sdk-contract.test.ts`.
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
