import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { buildApp } from '../src/app.js';

/**
 * Independently verify that the running stack serves bytes matching the
 * committed samples/manifest.
 *
 *  - single-part responses are hashed and compared to the manifest's
 *    literal expected sha256 values (manifest generated without the range
 *    engine), and Content-Length is checked against the real body;
 *  - multipart responses are compared against framing built HERE from the
 *    manifest's per-part hashes (not against src/range/multipart), so the
 *    check cannot silently agree with the encoder under test.
 *
 * Exits non-zero on any mismatch. Run: npm run verify:samples
 */

interface SliceAnswer {
  interval: string;
  length: number;
  sha256: string;
}
interface ManifestObject {
  id: string;
  file: string;
  contentType: string;
  size: number;
  sha256: string;
  documentedRequests: string[];
  expectedSlices: Record<string, SliceAnswer | { parts: SliceAnswer[] }>;
}

const sha = (b: Buffer): string => createHash('sha256').update(b).digest('hex');
const CRLF = '\r\n';

let failures = 0;
function check(name: string, cond: boolean, detail = ''): void {
  if (cond) {
    console.log(`  ok   ${name}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${name} ${detail}`);
  }
}

async function main(): Promise<void> {
  const manifestDir = join(process.cwd(), 'samples');
  const manifest = JSON.parse(
    readFileSync(join(manifestDir, 'manifest.json'), 'utf8'),
  ) as { objects: ManifestObject[] };

  const built = await buildApp({ databasePath: ':memory:', logPath: null });

  for (const obj of manifest.objects) {
    const data = readFileSync(join(manifestDir, obj.file));
    check(`${obj.id} file sha256`, sha(data) === obj.sha256);
    built.store.put({ id: obj.id, data, contentType: obj.contentType });

    for (const request of obj.documentedRequests) {
      const res = await built.app.inject({
        method: 'GET',
        url: `/objects/${obj.id}`,
        headers: { Range: request },
      });
      const declaredLength = Number(res.headers['content-length']);
      check(
        `${request} Content-Length==body (${declaredLength}==${res.rawPayload.length})`,
        declaredLength === res.rawPayload.length,
      );

      const expected = obj.expectedSlices[request]!;
      if ('parts' in expected) {
        // Independently frame the expected parts and compare byte-for-byte.
        const ct = String(res.headers['content-type'] ?? '');
        const bm = /boundary=([!-~]+)$/.exec(ct);
        check(`${request} multipart content-type`, bm !== null);
        if (!bm) continue;
        const boundary = bm[1]!;
        const chunks: Buffer[] = [];
        for (const part of expected.parts) {
          const [, s, e, total] = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(part.interval)!;
          chunks.push(
            Buffer.from(
              `--${boundary}${CRLF}Content-Type: ${obj.contentType}${CRLF}` +
                `Content-Range: bytes ${s}-${e}/${total}${CRLF}${CRLF}`,
              'ascii',
            ),
          );
          chunks.push(data.subarray(Number(s), Number(e) + 1));
          chunks.push(Buffer.from(CRLF, 'ascii'));
        }
        chunks.push(Buffer.from(`--${boundary}--${CRLF}`, 'ascii'));
        const expectedBody = Buffer.concat(chunks);
        check(`${request} multipart body byte-exact`, res.rawPayload.equals(expectedBody));
      } else {
        check(`${request} status 206`, res.statusCode === 206);
        check(
          `${request} content-range ${expected.interval}`,
          res.headers['content-range'] === expected.interval,
        );
        check(`${request} body sha256`, sha(res.rawPayload) === expected.sha256);
        check(`${request} body length`, res.rawPayload.length === expected.length);
      }
    }
  }

  await built.app.close();
  built.store.close();

  if (failures > 0) {
    console.error(`\n${failures} sample check(s) failed`);
    process.exit(1);
  }
  console.log('\nAll sample checks passed.');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
