/**
 * Quota and restriction configuration.
 *
 * Three independently enforced size quotas are required:
 *   1. per-part body   (maxPartBytes, maxFieldBytes)
 *   2. aggregate body  (maxTotalBytes)
 *   3. header section   (maxHeaderBytes)
 * plus a part-count cap. Uploads are additionally restricted by an extension
 * allow-list and a Content-Type allow-list; both are optional and independently
 * toggleable.
 */

export interface UploadRestrictions {
  /** Allowed lowercase extensions including the dot, e.g. ['.txt', '.png']. */
  allowedExtensions: readonly string[];
  /** Allowed exact Content-Type values for file parts. */
  allowedMimeTypes: readonly string[];
  /** Reject filenames containing control characters or path separators. */
  rejectPathTraversal: boolean;
}

export interface MultipartLimits {
  /** Max bytes of a single file-part body. */
  maxPartBytes: number;
  /** Max bytes of a single field-part value. */
  maxFieldBytes: number;
  /** Max summed bytes of all part bodies in one request. */
  maxTotalBytes: number;
  /** Max bytes of one part's header section (CRLF CRLF delimited). */
  maxHeaderBytes: number;
  /** Max number of parts in one request. */
  maxParts: number;
  /** Max bytes of the boundary parameter value. */
  maxBoundaryLength: number;
}

export interface MultipartConfig {
  limits: MultipartLimits;
  restrictions: UploadRestrictions;
  /** Directory under which per-request temp sub-directories are created. */
  tempDir: string;
  /**
   * Treat an isolated LF inside body/framing as a protocol error. RFC
   * multipart framing is CRLF based; enabling this (default) rejects bare LFs.
   */
  strictCrlf: boolean;
}

export const DEFAULT_LIMITS: MultipartLimits = {
  maxPartBytes: 5 * 1024 * 1024, // 5 MiB per file
  maxFieldBytes: 64 * 1024, // 64 KiB per field value
  maxTotalBytes: 16 * 1024 * 1024, // 16 MiB aggregate
  maxHeaderBytes: 8 * 1024, // 8 KiB header section
  maxParts: 32,
  maxBoundaryLength: 70, // RFC 2046 bchars max
};

export const DEFAULT_RESTRICTIONS: UploadRestrictions = {
  allowedExtensions: [
    '.txt',
    '.log',
    '.csv',
    '.json',
    '.png',
    '.jpg',
    '.jpeg',
    '.gif',
    '.pdf',
  ],
  allowedMimeTypes: [
    'text/plain',
    'text/csv',
    'application/json',
    'application/pdf',
    'image/png',
    'image/jpeg',
    'image/gif',
  ],
  rejectPathTraversal: true,
};

export function createDefaultConfig(
  overrides: Partial<MultipartConfig> = {},
): MultipartConfig {
  return {
    limits: { ...DEFAULT_LIMITS, ...overrides.limits },
    restrictions: { ...DEFAULT_RESTRICTIONS, ...overrides.restrictions },
    tempDir: overrides.tempDir ?? '.data/tmp',
    strictCrlf: overrides.strictCrlf ?? true,
  };
}
