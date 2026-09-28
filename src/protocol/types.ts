/**
 * Data contracts shared by parser, storage adapter, repository and HTTP layer.
 *
 * The parser never talks to SQLite or the file system directly. It emits
 * {@link PartMeta} descriptors and pushes body bytes through a
 * {@link PartSink}. The storage layer decides whether bytes are buffered
 * (small field values) or spooled to a per-request temp file (uploads).
 */

export type PartKind = 'field' | 'file';

/** Metadata resolved from a part's header section, before any body bytes. */
export interface PartMeta {
  /** Form field name (Content-Disposition `name`), always present. */
  name: string;
  /** `field` for ordinary form fields, `file` when a filename is present. */
  kind: PartKind;
  /** Original filename as decoded per RFC 5987 / RFC 7578. Absent for fields. */
  filename?: string;
  /** Content-Type of the part. Defaults to text/plain per RFC 7578. */
  contentType: string;
  /** Raw header lines exactly as received (CRLF stripped), for diagnostics. */
  headerLines: string[];
}

/** Final descriptor handed to the application after a part body completes. */
export interface CompletedPart {
  meta: PartMeta;
  /** Bytes received for this part. */
  size: number;
  /** Field value as UTF-8 string, present only for `field` parts. */
  value?: string;
  /** Absolute path of the spool file, present only for `file` parts. */
  tempPath?: string;
  /** Content hash (SHA-256 hex) of the part body. */
  sha256: string;
}

/** A fully parsed, validated submission that may be committed. */
export interface ParsedForm {
  parts: CompletedPart[];
  /** Total wire size of all part bodies (bytes), excluding framing. */
  totalSize: number;
}

/** Result of a successful durable commit. */
export interface CommittedSubmission {
  id: number;
  createdAt: string;
  fieldCount: number;
  fileCount: number;
  totalSize: number;
}

/**
 * Sink abstraction for one part body.
 *
 * Implementations MUST tolerate zero or more `write()` calls followed by
 * exactly one `end()` on success, or one `destroy()` on failure/cancel.
 */
export interface PartSink {
  write(chunk: Buffer): void;
  end(): Promise<FinalizedPart> | FinalizedPart;
  /** Release every resource owned by THIS part only. Must be idempotent. */
  destroy(): Promise<void> | void;
}

export interface FinalizedPart {
  size: number;
  sha256: string;
  /** Present iff the part spooled to disk. */
  tempPath?: string;
  /** Present iff the part was buffered in memory. */
  value?: string;
}

/** Lifecycle hooks the parser uses to announce protocol state transitions. */
export interface ParserObserver {
  onPartBegin?(meta: PartMeta): void;
  onPartChunk?(meta: PartMeta, bytes: number, runningTotal: number): void;
  onPartEnd?(part: CompletedPart): void;
  onBoundary?(kind: 'intermediate' | 'terminal', total: number): void;
}
