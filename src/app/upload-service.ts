/**
 * Upload orchestration: wires the raw HTTP entity stream to the parser,
 * enforces the terminating-boundary commit gate, and records run diagnostics.
 */

import type { IncomingMessage } from 'node:http';
import { ErrorCode, MultipartError } from '../protocol/errors.js';
import { parseMultipartContentType } from '../protocol/header-values.js';
import { MultipartParser, type ParseResult } from '../protocol/multipart-parser.js';
import type { AppConfig } from '../config.js';
import { RunLogger, newRunId, type Verdict } from '../diagnostics/run-logger.js';
import { UploadSession } from '../storage/upload-session.js';
import type { SubmissionRecord, SubmissionStore } from '../storage/submission-store.js';

export interface UploadOutcome {
  runId: string;
  record: SubmissionRecord;
  result: ParseResult;
}

function verdictFor(err: MultipartError): Verdict {
  return err.errorClass === 'INPUT_ERROR'
    ? 'INPUT_ERROR'
    : err.errorClass === 'STATE_CONFLICT'
      ? 'STATE_CONFLICT'
      : err.errorClass === 'RESOURCE_LIMIT'
        ? 'RESOURCE_LIMIT'
        : 'COMPUTE_FAILURE';
}

/**
 * Consume `req` fully and publish one submission.
 *
 * Rejects the promise with a {@link MultipartError} for any non-success.
 * Client disconnect / aborted stream yields UPLOAD_CANCELED (STATE_CONFLICT)
 * and removes request-local temp data only.
 */
export function processUpload(
  req: IncomingMessage,
  config: AppConfig,
  store: SubmissionStore,
  logger: RunLogger
): Promise<UploadOutcome> {
  return new Promise<UploadOutcome>((resolve, reject) => {
    const runId = newRunId('up');
    const started = Date.now();
    let contentType: string;
    let boundaryResolved: string;
    try {
      const rawContentType = req.headers['content-type'];
      if (!rawContentType || typeof rawContentType !== 'string') {
        throw new MultipartError(
          ErrorCode.MALFORMED_CONTENT_TYPE,
          'request is missing a Content-Type header',
          {}
        );
      }
      contentType = rawContentType;
      boundaryResolved = parseMultipartContentType(contentType).boundary;
    } catch (err) {
      const me = err instanceof MultipartError ? err : new MultipartError(ErrorCode.INTERNAL, String(err));
      logger.verdict(runId, verdictFor(me), { code: me.code, message: me.message }, undefined, undefined, Date.now() - started);
      reject(me);
      return;
    }

    const session = new UploadSession({
      runId,
      tmpRoot: config.tmpDir,
      filesDir: config.filesDir,
      limits: config.limits,
      policy: config.filePolicy,
      requireFilePart: config.requireFilePart,
      rejectDuplicateNames: true
    });
    const parser = new MultipartParser(boundaryResolved, config.limits, session);

    logger.open(runId, {
      method: req.method,
      contentType,
      boundaryLength: boundaryResolved.length,
      contentLength: req.headers['content-length'] ?? null,
      limits: config.limits
    });

    let settled = false;
    const progressTimer = setInterval(() => {
      logger.progress(runId, 'streaming', {
        state: parser.getState(),
        counters: parser.getCounters()
      });
    }, 250);
    progressTimer.unref();

    const fail = (err: unknown, canceled = false): void => {
      if (settled) return;
      settled = true;
      clearInterval(progressTimer);
      const me =
        err instanceof MultipartError
          ? err
          : new MultipartError(
              ErrorCode.IO_FAILURE,
              `request stream failure: ${(err as Error)?.message ?? String(err)}`,
              {}
            );
      parser.abort();
      session.discard();
      const verdict: Verdict = canceled
        ? 'CANCELED'
        : verdictFor(me);
      logger.verdict(
        runId,
        verdict,
        { code: canceled ? ErrorCode.UPLOAD_CANCELED : me.code, message: me.message },
        { state: parser.getState(), counters: parser.getCounters() },
        [...parser.getEvents()],
        Date.now() - started
      );
      if (canceled) {
        reject(
          new MultipartError(
            ErrorCode.UPLOAD_CANCELED,
            'upload was canceled before the terminating boundary was validated',
            { runId }
          )
        );
        return;
      }
      reject(me);
    };

    req.on('data', (chunk: Buffer) => {
      try {
        parser.write(chunk);
      } catch (err) {
        // Stop reading but do NOT destroy the socket: the client must receive
        // the structured 4xx. The route error handler closes the connection
        // after sending it, so unread body bytes cannot leak into another
        // request on a reused socket.
        req.pause();
        fail(err);
      }
    });

    req.on('end', () => {
      if (settled) return;
      try {
        const result = parser.end(); // throws unless terminating boundary seen
        const record = session.commit(store, result.counters); // commit gate
        settled = true;
        clearInterval(progressTimer);
        logger.verdict(
          runId,
          'SUCCESS',
          { code: 'OK', message: 'submission published after terminating boundary validation' },
          { counters: result.counters, parts: result.parts.map((p) => ({ index: p.index, name: p.name, size: p.size })) },
          [...parser.getEvents()],
          Date.now() - started
        );
        resolve({ runId, record, result });
      } catch (err) {
        req.pause();
        fail(err);
      }
    });

    req.on('error', (err) => fail(err));
    req.on('aborted', () => {
      // client closed the connection mid-upload
      fail(new Error('client aborted the request'), true);
    });
    req.on('close', () => {
      // 'close' fires after 'end' as well; only act when nothing settled and the
      // stream did not finish (no complete entity received).
      if (!settled && req.readableEnded === false && req.complete === false) {
        fail(new Error('connection closed before upload completed'), true);
      }
    });
  });
}
