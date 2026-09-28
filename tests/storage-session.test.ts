/**
 * Storage adapter tests: request-local temp dirs, file promotion, SQLite commit
 * gate, duplicate-name rejection and temp-resource reclamation after failures
 * and cancels.
 */

import { existsSync, mkdtempSync, readFileSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { ErrorCode, MultipartError } from '../src/protocol/errors.js';
import { MultipartParser } from '../src/protocol/multipart-parser.js';
import { DEFAULT_FILE_POLICY, DEFAULT_LIMITS } from '../src/protocol/types.js';
import { SubmissionStore } from '../src/storage/submission-store.js';
import { UploadSession } from '../src/storage/upload-session.js';
import { removeDir } from './helpers.js';

function makeMessage(
  boundary: string,
  parts: Array<{ name: string; body: Buffer; filename?: string; contentType?: string }>
): Buffer {
  const blocks = parts.map((p) => {
    let disp = `Content-Disposition: form-data; name="${p.name}"`;
    if (p.filename !== undefined) disp += `; filename="${p.filename}"`;
    const lines = [disp];
    if (p.contentType) lines.push(`Content-Type: ${p.contentType}`);
    return Buffer.concat([Buffer.from('\r\n' + lines.join('\r\n') + '\r\n\r\n'), p.body]);
  });
  const head = Buffer.from(`--${boundary}`);
  const sep = Buffer.from(`\r\n--${boundary}`);
  return Buffer.concat([head, Buffer.concat(blocks.map((b, i) => (i === 0 ? b : Buffer.concat([sep, b])))), Buffer.from(`\r\n--${boundary}--\r\n`)]);
}

describe('UploadSession + SubmissionStore commit gate', () => {
  let root: string;
  let tmpRoot: string;
  let filesDir: string;
  let dbPath: string;
  let store: SubmissionStore;

  beforeEach(() => {
    root = mkdtempSync(join(tmpdir(), 'mp-session-'));
    tmpRoot = join(root, 'tmp');
    filesDir = join(root, 'files');
    dbPath = join(root, 'app.sqlite');
    store = new SubmissionStore(dbPath);
  });

  afterEach(() => {
    store.close();
    removeDir(root);
  });

  function newSession(runId: string, requireFilePart = false): UploadSession {
    return new UploadSession({
      runId,
      tmpRoot,
      filesDir,
      limits: DEFAULT_LIMITS,
      policy: DEFAULT_FILE_POLICY,
      requireFilePart,
      rejectDuplicateNames: true
    });
  }

  function parse(session: UploadSession, boundary: string, data: Buffer) {
    const parser = new MultipartParser(boundary, DEFAULT_LIMITS, session);
    parser.write(data);
    return parser.end();
  }

  it('publishes nothing to SQLite or files dir until commit()', () => {
    const session = newSession('run-visible-1');
    const data = makeMessage('B', [
      { name: 'title', body: Buffer.from('hello') },
      { name: 'up', body: Buffer.from([1, 2, 3]), filename: 'a.txt', contentType: 'text/plain' }
    ]);
    parse(session, 'B', data);

    // Parsed but not committed: temp file exists, permanent dir empty, no row.
    expect(readdirSync(filesDir)).toEqual([]);
    expect(store.count().submissions).toBe(0);
    expect(existsSync(join(tmpRoot, 'run-visible-1'))).toBe(true);

    const counters = { wireBytes: 0, bodyBytes: 8, headerBytes: 0, parts: 2, fields: 1, files: 1 };
    const record = session.commit(store, counters);
    expect(store.getSubmission(record.id)?.filesCount).toBe(1);
    expect(readdirSync(filesDir)).toHaveLength(1);
    // request-local temp dir removed after successful commit
    expect(existsSync(join(tmpRoot, 'run-visible-1'))).toBe(false);
  });

  it('commit gate rejects COMMIT_BEFORE_COMPLETION if a part is still open', () => {
    const session = newSession('run-gate');
    session.openPart({
      name: 'x',
      effectiveFilename: null,
      filename: null,
      filenameStar: null,
      filenameStarCharset: null,
      contentType: null,
      disposition: null as never,
      rawHeaders: Buffer.alloc(0),
      index: 1,
      isFile: false
    });
    expect(() =>
      session.commit(store, { wireBytes: 0, bodyBytes: 0, headerBytes: 0, parts: 0, fields: 0, files: 0 })
    ).toThrowError(
      expect.objectContaining({ code: ErrorCode.COMMIT_BEFORE_COMPLETION, errorClass: 'STATE_CONFLICT' })
    );
    session.discard();
  });

  it('double commit is ALREADY_COMMITTED', () => {
    const session = newSession('run-double');
    parse(
      session,
      'B',
      makeMessage('B', [{ name: 'a', body: Buffer.from('1') }, { name: 'up', body: Buffer.from('xy'), filename: 'a.txt', contentType: 'text/plain' }])
    );
    const counters = { wireBytes: 0, bodyBytes: 3, headerBytes: 0, parts: 2, fields: 1, files: 1 };
    session.commit(store, counters);
    expect(() => session.commit(store, counters)).toThrowError(
      expect.objectContaining({ code: ErrorCode.ALREADY_COMMITTED })
    );
  });

  it('requireFilePart rejects field-only submissions with NO_FILE_PARTS', () => {
    const session = newSession('run-nofile', true);
    parse(session, 'B', makeMessage('B', [{ name: 'a', body: Buffer.from('1') }]));
    expect(() =>
      session.commit(store, { wireBytes: 0, bodyBytes: 1, headerBytes: 0, parts: 1, fields: 1, files: 0 })
    ).toThrowError(expect.objectContaining({ code: ErrorCode.NO_FILE_PARTS, errorClass: 'INPUT_ERROR' }));
    // failed commit cleaned its temp dir
    expect(existsSync(join(tmpRoot, 'run-nofile'))).toBe(false);
    expect(readdirSync(filesDir)).toEqual([]);
  });

  it('rejects duplicate field names', () => {
    const session = newSession('run-dup');
    const data = makeMessage('B', [
      { name: 'same', body: Buffer.from('1') },
      { name: 'same', body: Buffer.from('2') }
    ]);
    try {
      parse(session, 'B', data);
      throw new Error('expected DUPLICATE_PART_NAME');
    } catch (err) {
      expect((err as MultipartError).code).toBe(ErrorCode.DUPLICATE_PART_NAME);
    } finally {
      session.discard();
    }
    expect(existsSync(join(tmpRoot, 'run-dup'))).toBe(false);
  });

  it('discard after a parse failure removes ONLY this request temp data', () => {
    const s1 = newSession('run-keep');
    parse(
      s1,
      'B',
      makeMessage('B', [{ name: 'up', body: Buffer.from('keep'), filename: 'a.txt', contentType: 'text/plain' }])
    );
    s1.commit(store, { wireBytes: 0, bodyBytes: 4, headerBytes: 0, parts: 1, fields: 0, files: 1 });

    const s2 = newSession('run-fail');
    // malicious extension -> policy error at openPart of part 2
    const bad = makeMessage('B', [
      { name: 'ok', body: Buffer.from('z') },
      { name: 'up', body: Buffer.from('evil'), filename: 'x.exe', contentType: 'application/x-msdownload' }
    ]);
    try {
      parse(s2, 'B', bad);
      throw new Error('expected FILE_TYPE_REJECTED');
    } catch (err) {
      expect((err as MultipartError).code).toBe(ErrorCode.FILE_TYPE_REJECTED);
    } finally {
      s2.discard();
    }

    expect(existsSync(join(tmpRoot, 'run-fail'))).toBe(false);
    expect(readdirSync(filesDir)).toHaveLength(1); // run-keep file untouched
    expect(store.count().submissions).toBe(1);
  });

  it('promoted file bytes round-trip and match the recorded sha256', () => {
    const session = newSession('run-bytes');
    const payload = Buffer.from('the quick brown fox\n');
    parse(
      session,
      'B',
      makeMessage('B', [{ name: 'up', body: payload, filename: 'fox.txt', contentType: 'text/plain' }])
    );
    const record = session.commit(store, {
      wireBytes: 0,
      bodyBytes: payload.length,
      headerBytes: 0,
      parts: 1,
      fields: 0,
      files: 1
    });
    const row = record.parts[0]!;
    const stored = join(filesDir, row.storedName!);
    expect(readFileSync(stored)).toEqual(payload);
    const persisted = store.getSubmission(record.id)!;
    expect(persisted.parts[0]!.sha256).toBe(row.sha256);
    expect(persisted.parts[0]!.filename).toBe('fox.txt');
  });

  it('aborted/canceled upload leaves no temp dir and no rows', () => {
    const session = newSession('run-cancel');
    const parser = new MultipartParser('B', DEFAULT_LIMITS, session);
    parser.write(
      makeMessage('B', [{ name: 'up', body: Buffer.from('partial'), filename: 'a.txt', contentType: 'text/plain' }]).subarray(0, 30)
    );
    parser.abort();
    session.discard();
    expect(existsSync(join(tmpRoot, 'run-cancel'))).toBe(false);
    expect(readdirSync(filesDir)).toEqual([]);
    expect(store.count().submissions).toBe(0);
  });
});
