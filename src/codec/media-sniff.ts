/**
 * Media type detection from real content bytes ("magic numbers") — never from
 * filename extension, Content-Type headers or the declared image field.
 *
 * Supported formats: PNG, JPEG, GIF, WebP.
 */
import { ImageType } from "../config";

export interface SniffResult {
  type: ImageType;
  /** Human-readable evidence, e.g. "PNG signature 89 50 4e 47 + IHDR". */
  evidence: string;
}

function hex(bytes: number[], start = 0): string {
  return bytes.map((b) => b.toString(16).padStart(2, "0")).join(" ");
}

export function sniffMediaType(buf: Buffer): SniffResult | null {
  // PNG: 89 50 4E 47 0D 0A 1A 0A, IHDR chunk must follow.
  if (
    buf.length >= 24 &&
    buf[0] === 0x89 &&
    buf[1] === 0x50 &&
    buf[2] === 0x4e &&
    buf[3] === 0x47 &&
    buf[4] === 0x0d &&
    buf[5] === 0x0a &&
    buf[6] === 0x1a &&
    buf[7] === 0x0a &&
    buf.subarray(12, 16).toString("ascii") === "IHDR"
  ) {
    const width = buf.readUInt32BE(16);
    const height = buf.readUInt32BE(20);
    return {
      type: "image/png",
      evidence: `PNG 8-byte signature (${hex([...buf.subarray(0, 8)])}) + IHDR chunk ${width}x${height}`,
    };
  }

  // JPEG: starts FF D8 FF; next marker must be a valid SOI marker.
  if (buf.length >= 3 && buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) {
    return {
      type: "image/jpeg",
      evidence: `JPEG SOI marker ff d8 followed by marker ff ${buf[3].toString(16)}`,
    };
  }

  // GIF: "GIF87a" or "GIF89a".
  if (buf.length >= 6) {
    const sig = buf.subarray(0, 6).toString("ascii");
    if (sig === "GIF87a" || sig === "GIF89a") {
      return {
        type: "image/gif",
        evidence: `GIF signature ${JSON.stringify(sig)}`,
      };
    }
  }

  // WebP: "RIFF" .... "WEBP" "VP8 "/"VP8L"/"VP8X".
  if (
    buf.length >= 12 &&
    buf.subarray(0, 4).toString("ascii") === "RIFF" &&
    buf.subarray(8, 12).toString("ascii") === "WEBP"
  ) {
    const variant = buf.subarray(12, 16).toString("ascii");
    return {
      type: "image/webp",
      evidence: `WebP RIFF/WEBP container, ${JSON.stringify(variant)} bitstream`,
    };
  }

  return null;
}
