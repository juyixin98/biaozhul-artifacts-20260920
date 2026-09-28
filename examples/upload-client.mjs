/**
 * Dependency-free multipart client example.
 *
 * Run while the server is up (npm run dev):
 *   node examples/upload-client.mjs
 *
 * It hand-builds a multipart/form-data body — no SDK — and deliberately sends
 * the bytes in small, mis-aligned fragments so that boundary delimiters are
 * forced to straddle writes. This is the same property the parser test suite
 * verifies exhaustively; the example makes it observable over real HTTP.
 */

import http from 'node:http';

const HOST = process.env.HOST ?? '127.0.0.1';
const PORT = Number(process.env.PORT ?? 3000);
const BOUNDARY = '----exampleBoundary9';

function part(name, body, { filename, contentType } = {}) {
  const head = [
    `--${BOUNDARY}`,
    `Content-Disposition: form-data; name="${name}"` +
      (filename ? `; filename="${filename}"` : ''),
  ];
  if (contentType) head.push(`Content-Type: ${contentType}`);
  return Buffer.concat([
    Buffer.from(head.join('\r\n') + '\r\n\r\n', 'utf8'),
    Buffer.isBuffer(body) ? body : Buffer.from(body, 'utf8'),
    Buffer.from('\r\n', 'ascii'),
  ]);
}

const body = Buffer.concat([
  part('title', 'report generated'),
  part('file', 'line1\nline2\nline3\n', {
    filename: 'report.txt',
    contentType: 'text/plain',
  }),
  Buffer.from(`--${BOUNDARY}--\r\n`, 'ascii'),
]);

const req = http.request(
  {
    host: HOST,
    port: PORT,
    path: '/uploads',
    method: 'POST',
    headers: {
      'Content-Type': `multipart/form-data; boundary=${BOUNDARY}`,
      'Content-Length': body.length,
    },
  },
  (res) => {
    let data = '';
    res.setEncoding('utf8');
    res.on('data', (c) => (data += c));
    res.on('end', () => {
      console.log(`HTTP ${res.statusCode}`);
      console.log(data);
    });
  },
);

// Send with a deliberately awkward 7-byte stride and inter-write delay so
// every delimiter is very likely split across two TCP writes.
const STRIDE = 7;
let pos = 0;
function push() {
  if (pos >= body.length) {
    req.end();
    return;
  }
  const next = Math.min(body.length, pos + STRIDE);
  req.write(body.subarray(pos, next));
  pos = next;
  setTimeout(push, 1);
}
push();

req.on('error', (err) => {
  console.error('request failed:', err.message);
  process.exit(1);
});
