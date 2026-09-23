/**
 * Minimal Protocol Buffers wire codec, strict enough for IPLD dag-pb/UnixFS.
 *
 * Only what dag-pb uses:
 *   varint (wire 0), length-delimited (wire 2)
 * Rejected:
 *   groups (wire 3/4, deprecated in proto3), wire 1/6/7,
 *   field number 0, truncated input, trailing garbage,
 *   non-minimal varints (e.g. 0x80 0x00), 64-bit overflow
 *
 * This is a real decoder (tag/length/varint carry arithmetic), not a shape
 * check; the decoded tokens are fed to dag-pb canonical validation.
 */

export interface PbField {
  field: number;
  wireType: number;
  varint?: bigint;
  bytes?: Uint8Array;
}

class Reader {
  pos = 0;
  constructor(private readonly buf: Uint8Array) {}

  eof(): boolean {
    return this.pos >= this.buf.length;
  }

  byte(): number {
    if (this.pos >= this.buf.length) throw new Error('truncated protobuf message');
    return this.buf[this.pos++]!;
  }

  varint(): bigint {
    let result = 0n;
    let shift = 0n;
    const start = this.pos;
    for (;;) {
      if (shift > 63n) throw new Error('varint exceeds 64 bits');
      const b = this.byte();
      result |= BigInt(b & 0x7f) << shift;
      if ((b & 0x80) === 0) break;
      shift += 7n;
    }
    // Minimal-encoding rejection: re-encode and compare consumed bytes.
    const raw = this.buf.subarray(start, this.pos);
    let v = result;
    let i = 0;
    do {
      const expect = Number(v & 0x7fn) | (v > 0x7fn ? 0x80 : 0);
      if (raw[i] !== expect) throw new Error('non-minimal varint encoding');
      v >>= 7n;
      i++;
    } while (v > 0n);
    if (i !== raw.length) throw new Error('non-minimal varint encoding');
    return result;
  }

  bytesLen(n: number): Uint8Array {
    if (this.pos + n > this.buf.length) throw new Error('length-delimited field exceeds message');
    const out = this.buf.subarray(this.pos, this.pos + n);
    this.pos += n;
    return out;
  }
}

export function decodePb(input: Uint8Array): PbField[] {
  const r = new Reader(input);
  const fields: PbField[] = [];
  while (!r.eof()) {
    const tag = r.varint();
    if (tag === 0n) throw new Error('invalid protobuf tag zero');
    const field = Number(tag >> 3n);
    const wireType = Number(tag & 0x7n);
    if (field === 0) throw new Error('protobuf field number 0 is reserved');
    switch (wireType) {
      case 0: {
        fields.push({ field, wireType, varint: r.varint() });
        break;
      }
      case 2: {
        const len = Number(r.varint());
        fields.push({ field, wireType, bytes: Uint8Array.from(r.bytesLen(len)) });
        break;
      }
      case 3:
      case 4:
        throw new Error(`deprecated protobuf groups (wire type ${wireType}) are not supported`);
      case 1:
        r.bytesLen(8);
        throw new Error('fixed64 (wire type 1) fields are not supported by this codec');
      case 5:
        r.bytesLen(4);
        throw new Error('fixed32 (wire type 5) fields are not supported by this codec');
      default:
        throw new Error(`unknown wire type ${wireType}`);
    }
  }
  return fields;
}

class Writer {
  private parts: number[] = [];

  private varint(v: bigint | number): void {
    let n = typeof v === 'number' ? BigInt(v) : v;
    if (n < 0n) throw new Error('cannot encode negative protobuf varint');
    do {
      const b = Number(n & 0x7fn);
      n >>= 7n;
      this.parts.push(n > 0n ? b | 0x80 : b);
    } while (n > 0n);
  }

  tag(field: number, wire: number): void {
    this.varint((field << 3) | wire);
  }

  bytes(field: number, value: Uint8Array): void {
    this.tag(field, 2);
    this.varint(value.length);
    for (const b of value) this.parts.push(b);
  }

  uint64(field: number, value: bigint | number): void {
    this.tag(field, 0);
    this.varint(value);
  }

  append(value: Uint8Array): void {
    for (const b of value) this.parts.push(b);
  }

  finish(): Uint8Array {
    return Uint8Array.from(this.parts);
  }
}

export class PbWriter {
  private w = new Writer();
  bytes(field: number, value: Uint8Array): this {
    this.w.bytes(field, value);
    return this;
  }
  uint64(field: number, value: bigint | number): this {
    this.w.uint64(field, value);
    return this;
  }
  raw(value: Uint8Array): this {
    this.w.append(value);
    return this;
  }
  finish(): Uint8Array {
    return this.w.finish();
  }
}
