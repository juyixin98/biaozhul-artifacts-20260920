/**
 * Minimal Node.js (>=22) client example using the built-in fetch + FormData.
 *
 *   BASE=http://127.0.0.1:3000 node --no-warnings examples/node-client.mjs
 *
 * Demonstrates: one text field + one file part, then prints the receipt and
 * downloads the committed file byte-for-byte to verify the SHA-256 header.
 */

const BASE = process.env.BASE ?? 'http://127.0.0.1:3000';

const png = Buffer.from(
  '89504e470d0a1a0a0000000d4948445200000001000000010806000000' +
    '1f15c4890000000d49444154789c6360000002000100ffff0300000600' +
    '0557bfabd40000000049454e44ae426082',
  'hex'
);

const form = new FormData();
form.set('title', 'hello from node fetch');
form.set('pic', new File([png], 'pixel.png', { type: 'image/png' }));

const res = await fetch(`${BASE}/upload`, { method: 'POST', body: form });
if (!res.ok) {
  console.error('upload failed', res.status, await res.text());
  process.exit(1);
}
const receipt = await res.json();
console.log('uploaded:', JSON.stringify(receipt, null, 2));

const filePart = receipt.submission.parts.find((p) => p.type === 'file');
const dl = await fetch(`${BASE}${filePart.href}`);
const bytes = Buffer.from(await dl.arrayBuffer());
const crypto = await import('node:crypto');
const sha = crypto.createHash('sha256').update(bytes).digest('hex');
console.log('downloaded bytes:', bytes.length, 'sha256 matches:', sha === filePart.sha256);
