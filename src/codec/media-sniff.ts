/**
 * Media type sniffing from real content magic bytes (signatures) — never from
 * a filename or a claimed Content-Type. Signatures cover the media types an
 * NFT image/media field is allowed to reference here; unknown payloads fail
 * validation ("unknown encoding rejected", not silently accepted).
 */
export interface SniffResult {
  mime: string;
  ext: string;
  basis: string;
}

const u8 = (b: Buffer, off: number): number => b.readUInt8(off);

export function sniffMedia(bytes: Uint8Array): SniffResult | null {
  if (bytes.length === 0) return null;
  const b = Buffer.from(bytes);

  // PNG: 89 50 4E 47 0D 0A 1A 0A
  if (
    b.length >= 8 &&
    u8(b, 0) === 0x89 && u8(b, 1) === 0x50 && u8(b, 2) === 0x4e && u8(b, 3) === 0x47 &&
    u8(b, 4) === 0x0d && u8(b, 5) === 0x0a && u8(b, 6) === 0x1a && u8(b, 7) === 0x0a
  ) {
    const dims = pngDimensions(b);
    return { mime: 'image/png', ext: 'png', basis: `8-byte PNG signature 89504e470d0a1a0a${dims ? `; IHDR ${dims.w}x${dims.h}` : ''}` };
  }

  // JPEG: FF D8 FF ... FF D9
  if (b.length >= 3 && u8(b, 0) === 0xff && u8(b, 1) === 0xd8 && u8(b, 2) === 0xff) {
    return { mime: 'image/jpeg', ext: 'jpg', basis: 'SOI marker FFD8FF and terminating EOI FFD9' + (b[b.length - 2] === 0xff && b[b.length - 1] === 0xd9 ? ' present' : ' MISSING') };
  }

  // GIF: "GIF87a"/"GIF89a"
  if (
    b.length >= 6 &&
    b.subarray(0, 3).toString('ascii') === 'GIF' &&
    (b.subarray(3, 6).toString('ascii') === '87a' || b.subarray(3, 6).toString('ascii') === '89a')
  ) {
    return { mime: 'image/gif', ext: 'gif', basis: `ASCII magic ${b.subarray(0, 6).toString('ascii')}` };
  }

  // WEBP: RIFF .... WEBP
  if (
    b.length >= 12 &&
    b.subarray(0, 4).toString('ascii') === 'RIFF' &&
    b.subarray(8, 12).toString('ascii') === 'WEBP'
  ) {    return { mime: 'image/webp', ext: 'webp', basis: 'RIFF container with WEBP fourcc' };
  }

  // SVG: XML that parses with <svg as document element (BOM tolerated at read time)
  const svg = detectSvg(b);
  if (svg) return { mime: 'image/svg+xml', ext: 'svg', basis: svg };

  // MP4/ISO-BMFF: bytes 4..8 == "ftyp"
  if (b.length >= 12 && b.subarray(4, 8).toString('ascii') === 'ftyp') {
    const brand = b.subarray(8, 12).toString('ascii');
    return { mime: 'video/mp4', ext: 'mp4', basis: `ISO-BMFF ftyp box, major brand ${JSON.stringify(brand)}` };
  }

  // OGG: "OggS"
  if (b.length >= 4 && b.subarray(0, 4).toString('ascii') === 'OggS') {
    return { mime: 'application/ogg', ext: 'ogg', basis: 'capture pattern OggS' };
  }

  // MP3: ID3 tag or frame sync 0xFFEx/0xFFFx
  if (b.length >= 3 && b.subarray(0, 3).toString('ascii') === 'ID3') {
    return { mime: 'audio/mpeg', ext: 'mp3', basis: 'ID3v2 header' };
  }
  if (b.length >= 2 && u8(b, 0) === 0xff && (u8(b, 1) & 0xe0) === 0xe0) {
    return { mime: 'audio/mpeg', ext: 'mp3', basis: 'MPEG audio frame sync 0xFFE' };
  }

  // WAV: RIFF .... WAVE
  if (
    b.length >= 12 &&
    b.subarray(0, 4).toString('ascii') === 'RIFF' &&
    b.subarray(8, 12).toString('ascii') === 'WAVE'
  ) {
    return { mime: 'audio/wav', ext: 'wav', basis: 'RIFF container with WAVE fourcc' };
  }

  // PDF: "%PDF-"
  if (b.length >= 5 && b.subarray(0, 5).toString('ascii') === '%PDF-') {
    return { mime: 'application/pdf', ext: 'pdf', basis: `header ${b.subarray(0, 8).toString('ascii')}` };
  }

  return null;
}

/** Read IHDR width/height to prove the signature block is structurally present. */
function pngDimensions(b: Buffer): { w: number; h: number } | null {
  // IHDR must be the first chunk: sig(8) + length(4) + "IHDR"(4) + w(4) + h(4)
  if (b.length < 24 || b.subarray(12, 16).toString('ascii') !== 'IHDR') return null;
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) };
}

/** Real SVG detection: strip BOM/XML decl/doctype/comments and require an <svg> root. */
function detectSvg(b: Buffer): string | null {
  let text: string;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(b);
  } catch {
    return null;
  }
  const trimmed = text.trimStart();
  if (!trimmed.startsWith('<')) return null;
  // Remove comments, processing instructions and DOCTYPE, then look at root tag.
  const stripped = trimmed
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/<\?[\s\S]*?\?>/g, '')
    .replace(/<!DOCTYPE[\s\S]*?>/gi, '')
    .trimStart();
  if (/^<svg[\s>]/i.test(stripped)) {
    return 'well-formed XML prefix whose document element is <svg>';
  }
  return null;
}
