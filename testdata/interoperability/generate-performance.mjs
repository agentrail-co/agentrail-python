// Run from the repository root: node testdata/interoperability/generate-performance.mjs
// Uses node:crypto and native JSON.stringify, as agentrail-node's performance.ts does,
// independently of the Python encoder.
import fs from 'node:fs';
import { createHmac } from 'node:crypto';

const fixtures = [
  { name: 'ascii', hashKey: 'synthetic-second-key', namespaceId: '11111111-1111-4111-8111-111111111111', instanceRef: 'delivery-attempt-1' },
  { name: 'unicode-nul-controls', hashKey: 'synthetic-密钥-🔑', namespaceId: '22222222-2222-4222-9222-222222222222', instanceRef: 'é/😀\u0000"\\ \u001f<>&' },
];
for (const f of fixtures)
  f.hash = createHmac('sha256', f.hashKey)
    .update(JSON.stringify(['agentrail.operation', f.namespaceId, f.instanceRef]))
    .digest('hex');
fs.writeFileSync('testdata/interoperability/performance.json', JSON.stringify(fixtures, null, 2) + '\n');
