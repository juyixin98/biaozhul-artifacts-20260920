/**
 * Request orchestration (execution kernel wiring).
 *
 * One {@link UploadService.handleRequest} call is one multipart request:
 *
 *   parse envelope
 *     -> per-request TempStore (isolated directory)
 *       -> stream chunks into MultipartParser
 *         -> ONLY after the terminal boundary is verified:
 *              repository.commitAsync() (one SQLite transaction)
 *     -> finally: TempStore.cleanup() (this request's files only)
 *
 * Any error at any stage: parsed data is not committed, temp files are
 * removed, and the run logger records the exact failure class.
 */

import { randomUUID } from 'node:crypto';
import type { IncomingMessage } from 'node:http';
import { MultipartParser } from '../protocol/parser.js';
import { parseMultipartContentType } from '../protocol/content-type.js';
import {
  isMultipartError,
  stateError,
  type MultipartError,
} from '../protocol/errors.js';
import type { MultipartConfig } from '../protocol/config.js';
import type { CommittedSubmission } from '../protocol/types.js';
import { TempStore } from './temp-store.js';
import { SubmissionRepository } from './repository.js';
import { RunLogger, RunRegistry } from '../server/diagnostics.js';

export interface UploadResult {
  runId: string;
  submission: CommittedSubmission;
}

/** Error surface augmented with the run id after a request fails. */
export type RunTaggedError = MultipartError & { runId?: string };

export class UploadService {
  constructor(
    private readonly config: MultipartConfig,
    private readonly repo: SubmissionRepository,
    private readonly logPath?: string,
    private readonly registry?: RunRegistry,
  ) {}

  /**
   * Consume a raw request stream end-to-end.
   * @throws MultipartError of one of the four documented error classes
   */
  async handleRequest(req: IncomingMessage): Promise<UploadResult> {
    const runId = randomUUID();
    const logger = new RunLogger(runId, this.logPath);
    const temp = new TempStore(this.config.tempDir, runId);
    let parser: MultipartParser | null = null;
    let settled = false;

    const cleanup = async (): Promise<void> => {
      if (parser) await parser.abort().catch(() => undefined);
      await temp.cleanup();
    };

    try {
      const envelope = parseMultipartContentType(
        req.headers['content-type'],
        this.config.limits,
      );
      logger.envelope(envelope.boundary.length);

      await temp.prepare();

      let partIndex = -1;
      parser = new MultipartParser({
        boundaryDelimiter: envelope.boundaryDelimiter,
        limits: this.config.limits,
        restrictions: this.config.restrictions,
        strictCrlf: this.config.strictCrlf,
        createSink: (meta) => temp.createSink(meta),
        observer: {
          onPartBegin: (meta) => {
            partIndex += 1;
            logger.partBegin(meta, partIndex);
          },
          onPartChunk: (_meta, bytes, total) => logger.progress(partIndex, bytes, total),
          onBoundary: (kind, total) => logger.boundary(kind, total),
        },
      });

      // Stream the request body via async iteration, which gives correct
      // backpressure and a single terminal 'end' for free — avoiding the
      // pause()/resume()/'end' races that occur when an entire body lands in
      // one data event. Each write() is also serialized inside the parser.
      try {
        for await (const chunk of req) {
          await parser.write(chunk as Buffer);
        }
      } catch (err) {
        // A protocol/limit error is the real cause even if the framework has
        // already torn down the socket (req.aborted becomes true as the error
        // response is sent), so classify MultipartError before the abort.
        if (isMultipartError(err)) throw err as MultipartError;
        if (req.aborted) {
          throw stateError('ALREADY_ABORTED', 'upload cancelled by the client');
        }
        throw stateError('ALREADY_ABORTED', 'request stream errored before completion', {
          reason: (err as Error).message,
        });
      }
      if (req.aborted) {
        throw stateError('ALREADY_ABORTED', 'upload cancelled by the client');
      }

      // Visibility gate: finish() succeeds only with a verified terminator.
      const form = await parser.finish();

      const submission = await this.repo.commitAsync(form, async (part) => {
        if (!part.tempPath) {
          throw stateError('PART_NOT_OPEN', 'file part was not spooled', {
            field: part.meta.name,
          });
        }
        return temp.readPartFile(part.tempPath);
      });
      settled = true;
      logger.commit(submission);
      return { runId, submission };
    } catch (err) {
      logger.fail(err);
      if (isMultipartError(err)) {
        (err as RunTaggedError).runId = runId;
      }
      throw err;
    } finally {
      await cleanup();
      this.registry?.add(logger);
      void settled;
    }
  }
}
