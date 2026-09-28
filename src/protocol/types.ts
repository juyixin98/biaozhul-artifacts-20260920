/**
 * Declarative limits and file policy for one multipart upload.
 *
 * Three independent size budgets are tracked separately:
 *  - per-part body   ({@link Limits.maxFieldSize} / {@link Limits.maxFileSize})
 *  - total body      ({@link Limits.maxTotalSize})
 *  - per-part header block ({@link Limits.maxHeaderSize})
 *
 * Files are *restricted*: only {@link FilePolicy.allowedExtensions} and
 * {@link FilePolicy.allowedMimeTypes} (when lists are non-empty) are accepted,
 * plus a hard {@link FilePolicy.maxFilenameLength}.
 */

export interface Limits {
  /** max bytes of a single field (`name=...`) part body */
  maxFieldSize: number;
  /** max bytes of a single file part body */
  maxFileSize: number;
  /** max bytes summed across ALL part bodies (excludes delimiters/headers) */
  maxTotalSize: number;
  /** max bytes of one part's header block (CRLFCRLF inclusive) */
  maxHeaderSize: number;
  /** max total number of parts (fields + files) */
  maxParts: number;
  /** max number of field parts */
  maxFields: number;
  /** max number of file parts */
  maxFiles: number;
}

export interface FilePolicy {
  /** include the leading dot, lower-case, e.g. ".png"; empty list = allow any */
  allowedExtensions: string[];
  /** exact MIME types allowed; empty list = do not restrict on MIME */
  allowedMimeTypes: string[];
  maxFilenameLength: number;
}

export const DEFAULT_LIMITS: Limits = {
  maxFieldSize: 64 * 1024,
  maxFileSize: 5 * 1024 * 1024,
  maxTotalSize: 20 * 1024 * 1024,
  maxHeaderSize: 8 * 1024,
  maxParts: 100,
  maxFields: 60,
  maxFiles: 10
};

export const DEFAULT_FILE_POLICY: FilePolicy = {
  allowedExtensions: ['.txt', '.png', '.jpg', '.jpeg', '.gif', '.pdf'],
  allowedMimeTypes: [
    'text/plain',
    'image/png',
    'image/jpeg',
    'image/gif',
    'application/pdf'
  ],
  maxFilenameLength: 200
};

/** Normalized representation of one parsed part, handed from parser to storage. */
export interface PartInfo {
  /** form field name (Content-Disposition `name`), never empty */
  name: string;
  /** present iff disposition is `form-data` with a `filename` parameter */
  filename: string | null;
  /** `filename*` RFC 5987 value (percent-decoded UTF-8), independent of filename */
  filenameStar: string | null;
  /** charset tag of filename*, always "UTF-8" when accepted */
  filenameStarCharset: string | null;
  /** part Content-Type, defaulted to text/plain for fields per RFC 7578 §4.4 */
  contentType: string | null;
  /** accumulated raw header bytes (for diagnostics / replay), CRLF-normalized */
  rawHeaders: Buffer;
  /** ordinal position, 1-based, in body order */
  index: number;
  /** byte length of the part body, filled at part end */
  size: number;
  /** sha-256 hex of the part body, filled at part end */
  sha256: string;
}
