/**
 * File-part restriction policy enforcement.
 *
 * A part is a "file" when its Content-Disposition carries an effective
 * filename (filename* wins over filename). Such a part must pass:
 *  - filename length bound
 *  - safe basename rules (no separators, no "..", no control chars)
 *  - extension allow-list (when configured)
 *  - MIME allow-list (when configured; a file part declaring no Content-Type
 *    is rejected rather than guessed)
 */

import { ErrorCode, MultipartError } from '../protocol/errors.js';
import { safeBasename } from '../protocol/header-values.js';
import type { FilePolicy, PartInfo } from '../protocol/types.js';
import type { PartMeta } from '../protocol/part-headers.js';

export function enforceFilePolicy(meta: PartMeta, policy: FilePolicy): { extension: string } {
  const filename = safeBasename(meta.effectiveFilename ?? '', 'filename');
  if (filename.length > policy.maxFilenameLength) {
    throw new MultipartError(
      ErrorCode.FILE_TYPE_REJECTED,
      `filename length ${filename.length} exceeds ${policy.maxFilenameLength}`,
      { filename, maxFilenameLength: policy.maxFilenameLength }
    );
  }
  const dot = filename.lastIndexOf('.');
  const extension = dot > 0 ? filename.slice(dot).toLowerCase() : '';
  if (policy.allowedExtensions.length > 0) {
    if (extension === '' || !policy.allowedExtensions.includes(extension)) {
      throw new MultipartError(
        ErrorCode.FILE_TYPE_REJECTED,
        `file extension "${extension || '(none)'}" is not allowed`,
        { filename, extension, allowed: policy.allowedExtensions }
      );
    }
  }
  if (policy.allowedMimeTypes.length > 0) {
    if (!meta.contentType) {
      throw new MultipartError(
        ErrorCode.FILE_TYPE_REJECTED,
        'file part is missing a Content-Type header',
        { filename }
      );
    }
    if (!policy.allowedMimeTypes.includes(meta.contentType)) {
      throw new MultipartError(
        ErrorCode.FILE_TYPE_REJECTED,
        `MIME type "${meta.contentType}" is not allowed for file uploads`,
        { filename, contentType: meta.contentType, allowed: policy.allowedMimeTypes }
      );
    }
  }
  return { extension };
}

/** Summary used in HTTP receipts and diagnostic records. */
export function fileReceipt(info: PartInfo, storedName: string): Record<string, unknown> {
  return {
    field: info.name,
    filename: info.filename,
    storedName,
    contentType: info.contentType,
    size: info.size,
    sha256: info.sha256,
    partIndex: info.index
  };
}
