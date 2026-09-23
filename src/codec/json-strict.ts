/**
 * Strict JSON parser implemented from ECMA-404 / RFC 8259 grammar — this does
 * real tokenizing and parsing instead of accepting "JSON-shaped" strings:
 *
 *  - input must first decode as strict UTF-8 (fatal decoder; lone surrogates
 *    written as CESU-8/modified UTF-8 bytes are rejected upstream)
 *  - duplicate keys in the same object are rejected
 *  - only the JSON grammar is accepted: objects, arrays, strings, numbers,
 *    true/false/null — no NaN/Infinity, no comments, no trailing commas
 *  - numbers are parsed and bounds-checked (bigint when they exceed 2^53)
 */

export type JsonValue =
  | { kind: 'object'; entries: [string, JsonValue][] }
  | { kind: 'array'; items: JsonValue[] }
  | { kind: 'string'; value: string }
  | { kind: 'number'; raw: string; value: number | bigint }
  | { kind: 'boolean'; value: boolean }
  | { kind: 'null' };

class Parser {
  private pos = 0;
  constructor(private readonly s: string) {}

  parse(): JsonValue {
    this.ws();
    const v = this.value();
    this.ws();
    if (this.pos !== this.s.length) throw new Error(`unexpected character ${JSON.stringify(this.s[this.pos])} at offset ${this.pos}`);
    return v;
  }

  private ws(): void {
    while (this.pos < this.s.length) {
      const c = this.s.charCodeAt(this.pos);
      if (c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d) this.pos++;
      else break;
    }
  }

  private expect(ch: string): void {
    if (this.s[this.pos] !== ch) {
      throw new Error(`expected ${JSON.stringify(ch)} at offset ${this.pos}, found ${JSON.stringify(this.s[this.pos] ?? '<eof>')}`);
    }
    this.pos++;
  }

  private value(): JsonValue {
    this.ws();
    const ch = this.s[this.pos];
    switch (ch) {
      case '{': return this.object();
      case '[': return this.array();
      case '"': return { kind: 'string', value: this.string() };
      case 't': return this.literal('true', true);
      case 'f': return this.literal('false', false);
      case 'n': return this.literal('null', null);
      default:
        if (ch === '-' || (ch !== undefined && ch >= '0' && ch <= '9')) return this.number();
        throw new Error(`unexpected token ${JSON.stringify(ch ?? '<eof>')} at offset ${this.pos}`);
    }
  }

  private literal(lit: string, value: boolean | null): JsonValue {
    if (!this.s.startsWith(lit, this.pos)) throw new Error(`invalid literal at offset ${this.pos}`);
    this.pos += lit.length;
    return value === null ? { kind: 'null' } : { kind: 'boolean', value };
  }

  private object(): JsonValue {
    this.expect('{');
    this.ws();
    const entries: [string, JsonValue][] = [];
    const seen = new Set<string>();
    if (this.s[this.pos] === '}') {
      this.pos++;
      return { kind: 'object', entries };
    }
    for (;;) {
      this.ws();
      if (this.s[this.pos] !== '"') throw new Error(`expected string key at offset ${this.pos}`);
      const key = this.string();
      if (seen.has(key)) throw new Error(`duplicate JSON object key ${JSON.stringify(key)}`);
      seen.add(key);
      this.ws();
      this.expect(':');
      entries.push([key, this.value()]);
      this.ws();
      const c = this.s[this.pos];
      if (c === ',') { this.pos++; continue; }
      if (c === '}') { this.pos++; break; }
      throw new Error(`expected ',' or '}' at offset ${this.pos}`);
    }
    return { kind: 'object', entries };
  }

  private array(): JsonValue {
    this.expect('[');
    this.ws();
    const items: JsonValue[] = [];
    if (this.s[this.pos] === ']') {
      this.pos++;
      return { kind: 'array', items };
    }
    for (;;) {
      items.push(this.value());
      this.ws();
      const c = this.s[this.pos];
      if (c === ',') { this.pos++; continue; }
      if (c === ']') { this.pos++; break; }
      throw new Error(`expected ',' or ']' at offset ${this.pos}`);
    }
    return { kind: 'array', items };
  }

  private string(): string {
    this.expect('"');
    let out = '';
    while (this.pos < this.s.length) {
      const code = this.s.charCodeAt(this.pos);
      const ch = this.s[this.pos]!;
      if (code === 0x22) { this.pos++; return out; }
      if (code === 0x5c) {
        this.pos++;
        const e = this.s[this.pos];
        switch (e) {
          case '"': out += '"'; this.pos++; break;
          case '\\': out += '\\'; this.pos++; break;
          case '/': out += '/'; this.pos++; break;
          case 'b': out += '\b'; this.pos++; break;
          case 'f': out += '\f'; this.pos++; break;
          case 'n': out += '\n'; this.pos++; break;
          case 'r': out += '\r'; this.pos++; break;
          case 't': out += '\t'; this.pos++; break;
          case 'u': {
            const hex = this.s.slice(this.pos + 1, this.pos + 5);
            if (!/^[0-9a-fA-F]{4}$/.test(hex)) throw new Error(`invalid \\u escape at offset ${this.pos}`);
            let codeUnit = parseInt(hex, 16);
            this.pos += 5;
            if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
              // high surrogate must be followed by low surrogate
              if (this.s[this.pos] !== '\\' || this.s[this.pos + 1] !== 'u') {
                throw new Error('lone high surrogate in JSON string');
              }
              const hex2 = this.s.slice(this.pos + 2, this.pos + 6);
              if (!/^[0-9a-fA-F]{4}$/.test(hex2)) throw new Error('invalid low surrogate escape');
              const low = parseInt(hex2, 16);
              if (low < 0xdc00 || low > 0xdfff) throw new Error('high surrogate not followed by low surrogate');
              this.pos += 6;
              codeUnit = 0x10000 + ((codeUnit - 0xd800) << 10) + (low - 0xdc00);
            } else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
              throw new Error('lone low surrogate in JSON string');
            }
            out += String.fromCodePoint(codeUnit);
            break;
          }
          default:
            throw new Error(`invalid escape ${JSON.stringify(e)} at offset ${this.pos}`);
        }
      } else if (code < 0x20) {
        throw new Error(`unescaped control character U+${code.toString(16).padStart(4, '0')} in JSON string`);
      } else {
        out += ch;
        this.pos++;
      }
    }
    throw new Error('unterminated JSON string');
  }

  private number(): JsonValue {
    const start = this.pos;
    if (this.s[this.pos] === '-') this.pos++;
    const intStart = this.pos;
    if (this.s[this.pos] === '0') {
      this.pos++;
    } else {
      if (!(this.s[this.pos]! >= '1' && this.s[this.pos]! <= '9')) throw new Error(`invalid number at offset ${start}`);
      while (this.pos < this.s.length && this.s[this.pos]! >= '0' && this.s[this.pos]! <= '9') this.pos++;
    }
    if (intStart === this.pos || (this.s[start] === '-' && this.pos === start + 1)) {
      throw new Error(`invalid number at offset ${start}`);
    }
    let isFloat = false;
    if (this.s[this.pos] === '.') {
      isFloat = true;
      this.pos++;
      const fracStart = this.pos;
      while (this.pos < this.s.length && this.s[this.pos]! >= '0' && this.s[this.pos]! <= '9') this.pos++;
      if (this.pos === fracStart) throw new Error(`invalid fraction at offset ${start}`);
    }
    if (this.s[this.pos] === 'e' || this.s[this.pos] === 'E') {
      isFloat = true;
      this.pos++;
      if (this.s[this.pos] === '+' || this.s[this.pos] === '-') this.pos++;
      const expStart = this.pos;
      while (this.pos < this.s.length && this.s[this.pos]! >= '0' && this.s[this.pos]! <= '9') this.pos++;
      if (this.pos === expStart) throw new Error(`invalid exponent at offset ${start}`);
    }
    const raw = this.s.slice(start, this.pos);
    if (isFloat) {
      const n = Number(raw);
      if (!Number.isFinite(n)) throw new Error(`number ${raw} is not finite`);
      return { kind: 'number', raw, value: n };
    }
    const asNum = Number(raw);
    if (Number.isSafeInteger(asNum)) return { kind: 'number', raw, value: asNum };
    return { kind: 'number', raw, value: BigInt(raw) };
  }
}

export function parseStrictJson(bytes: Uint8Array): JsonValue {
  // Strict UTF-8 decode first: invalid sequences/lone surrogate encodings fail here.
  const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  return new Parser(text).parse();
}

export function jsonToNative(v: JsonValue): unknown {
  switch (v.kind) {
    case 'object': return Object.fromEntries(v.entries.map(([k, val]) => [k, jsonToNative(val)]));
    case 'array': return v.items.map(jsonToNative);
    case 'string': return v.value;
    case 'number': return v.value;
    case 'boolean': return v.value;
    case 'null': return null;
  }
}
