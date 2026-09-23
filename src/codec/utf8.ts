/**
 * Strict UTF-8 decoder.
 *
 * Buffer.toString('utf8') silently replaces invalid byte sequences with
 * U+FFFD, which would make a "non-UTF-8" metadata block look valid. This
 * decoder implements RFC 3629 exactly and throws on:
 *   - continuation bytes with no leader;
 *   - truncated multi-byte sequences;
 *   - overlong encodings;
 *   - surrogates U+D800–U+DFFF;
 *   - code points above U+10FFFF.
 */
export class InvalidUtf8Error extends Error {
  constructor(
    public readonly offset: number,
    message: string
  ) {
    super(`invalid UTF-8 at byte offset ${offset}: ${message}`);
    this.name = "InvalidUtf8Error";
  }
}

export function decodeUtf8Strict(bytes: Buffer): string {
  let out = "";
  let i = 0;
  while (i < bytes.length) {
    const b0 = bytes[i];
    if (b0 < 0x80) {
      out += String.fromCharCode(b0);
      i += 1;
      continue;
    }

    let cp: number;
    let len: number;
    if ((b0 & 0xe0) === 0xc0) {
      // 2-byte sequence
      if (b0 === 0xc0 || b0 === 0xc1) throw new InvalidUtf8Error(i, "overlong encoding");
      len = 2;
      cp = b0 & 0x1f;
    } else if ((b0 & 0xf0) === 0xe0) {
      len = 3;
      cp = b0 & 0x0f;
    } else if ((b0 & 0xf8) === 0xf0) {
      if (b0 > 0xf4) throw new InvalidUtf8Error(i, "code point above U+10FFFF");
      len = 4;
      cp = b0 & 0x07;
    } else {
      throw new InvalidUtf8Error(i, "invalid leading byte");
    }

    if (i + len > bytes.length) {
      throw new InvalidUtf8Error(i, "truncated multi-byte sequence");
    }
    for (let j = 1; j < len; j++) {
      const bj = bytes[i + j];
      if ((bj & 0xc0) !== 0x80) {
        throw new InvalidUtf8Error(i + j, "expected continuation byte");
      }
      cp = (cp << 6) | (bj & 0x3f);
    }

    // Overlong checks for 3/4-byte forms.
    if (len === 3 && cp < 0x800) throw new InvalidUtf8Error(i, "overlong encoding");
    if (len === 4 && cp < 0x10000) throw new InvalidUtf8Error(i, "overlong encoding");
    if (cp >= 0xd800 && cp <= 0xdfff) {
      throw new InvalidUtf8Error(i, "UTF-8 encodes a surrogate code point");
    }
    if (cp > 0x10ffff) {
      throw new InvalidUtf8Error(i, "code point above U+10FFFF");
    }

    if (cp <= 0xffff) {
      out += String.fromCharCode(cp);
    } else {
      const v = cp - 0x10000;
      out += String.fromCharCode(0xd800 + (v >> 10), 0xdc00 + (v & 0x3ff));
    }
    i += len;
  }
  return out;
}
