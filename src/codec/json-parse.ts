/**
 * Strict JSON parser.
 *
 * Why not JSON.parse? It accepts duplicate object keys (last wins, silently
 * discarding data) and happily decodes strings containing unpaired UTF-16
 * surrogates. This is a full recursive-descent parser (RFC 8259) that:
 *
 *   - rejects duplicate keys within one object;
 *   - rejects unpaired/lone UTF-16 surrogate escapes;
 *   - rejects leading '+' and other non-numeric tokens;
 *   - enforces single top-level value with no trailing garbage;
 *   - requires UTF-8 validity at the byte layer before parsing (see
 *     decodeUtf8Strict, used by the metadata verifier).
 */
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface JsonParseResult {
  value: JsonValue;
}

class Scanner {
  pos = 0;
  constructor(private readonly s: string) {}

  error(msg: string): never {
    throw new SyntaxError(`JSON parse error at offset ${this.pos}: ${msg}`);
  }

  skipWs(): void {
    while (this.pos < this.s.length) {
      const c = this.s.charCodeAt(this.pos);
      if (c === 0x20 || c === 0x09 || c === 0x0a || c === 0x0d) this.pos++;
      else break;
    }
  }

  parseValue(): JsonValue {
    this.skipWs();
    if (this.pos >= this.s.length) this.error("unexpected end of input");
    const c = this.s[this.pos];
    switch (c) {
      case '"':
        return this.parseString();
      case "{":
        return this.parseObject();
      case "[":
        return this.parseArray();
      case "t":
        return this.parseLiteral("true", true);
      case "f":
        return this.parseLiteral("false", false);
      case "n":
        return this.parseLiteral("null", null);
      default:
        return this.parseNumber();
    }
  }

  private parseLiteral(lit: string, value: JsonValue): JsonValue {
    if (this.s.startsWith(lit, this.pos)) {
      this.pos += lit.length;
      return value;
    }
    this.error(`invalid literal (expected '${lit}')`);
  }

  private parseNumber(): number {
    const start = this.pos;
    if (this.s[this.pos] === "-") this.pos++;
    if (this.pos >= this.s.length) this.error("invalid number");
    if (this.s[this.pos] === "0") {
      this.pos++;
      // Leading zeros (01) are illegal.
      if (this.pos < this.s.length && isDigit(this.s.charCodeAt(this.pos))) {
        this.error("leading zeros are not allowed");
      }
    } else if (isDigit1to9(this.s.charCodeAt(this.pos))) {
      while (this.pos < this.s.length && isDigit(this.s.charCodeAt(this.pos))) this.pos++;
    } else {
      this.error(this.s[this.pos] === "+" ? "leading '+' is not allowed" : "invalid number");
    }
    if (this.s[this.pos] === ".") {
      this.pos++;
      if (!isDigit(this.s.charCodeAt(this.pos))) this.error("digit expected after '.'");
      while (this.pos < this.s.length && isDigit(this.s.charCodeAt(this.pos))) this.pos++;
    }
    if (this.s[this.pos] === "e" || this.s[this.pos] === "E") {
      this.pos++;
      if (this.s[this.pos] === "+" || this.s[this.pos] === "-") this.pos++;
      if (!isDigit(this.s.charCodeAt(this.pos))) this.error("digit expected in exponent");
      while (this.pos < this.s.length && isDigit(this.s.charCodeAt(this.pos))) this.pos++;
    }
    const text = this.s.slice(start, this.pos);
    const n = Number(text);
    if (!Number.isFinite(n)) this.error("number out of range");
    return n;
  }

  private parseString(): string {
    this.pos++; // opening quote
    let out = "";
    while (this.pos < this.s.length) {
      const ch = this.s[this.pos];
      if (ch === '"') {
        this.pos++;
        return out;
      }
      if (ch === "\\") {
        this.pos++;
        const esc = this.s[this.pos];
        switch (esc) {
          case '"':
          case "\\":
          case "/":
            out += esc;
            this.pos++;
            break;
          case "b":
            out += "\b";
            this.pos++;
            break;
          case "f":
            out += "\f";
            this.pos++;
            break;
          case "n":
            out += "\n";
            this.pos++;
            break;
          case "r":
            out += "\r";
            this.pos++;
            break;
          case "t":
            out += "\t";
            this.pos++;
            break;
          case "u": {
            // Dispatch left this.pos on 'u'; rewind to the backslash so the
            // helper can consume the whole six-character escape uniformly.
            this.pos--;
            const cp = this.readHex4();
            if (cp >= 0xd800 && cp <= 0xdbff) {
              // High surrogate: must be immediately followed by a \uXXXX low
              // surrogate. readHex4 consumes the entire '\uXXXX' token.
              if (this.s[this.pos] !== "\\" || this.s[this.pos + 1] !== "u") {
                this.error("lone high surrogate escape");
              }
              const lo = this.readHex4();
              if (lo < 0xdc00 || lo > 0xdfff) {
                this.error("invalid low surrogate escape");
              }
              out += String.fromCharCode(cp, lo);
            } else if (cp >= 0xdc00 && cp <= 0xdfff) {
              this.error("lone low surrogate escape");
            } else {
              out += String.fromCharCode(cp);
            }
            break;
          }
          default:
            this.error(`invalid escape '\\${esc ?? ""}'`);
        }
      } else {
        const code = ch.charCodeAt(0);
        if (code < 0x20) this.error("unescaped control character in string");
        out += ch;
        this.pos++;
      }
    }
    this.error("unterminated string");
  }

  private readHex4(): number {
    // On entry this.pos points at the BACKSLASH. Consume the six-char
    // \uXXXX escape, leaving this.pos at the first character after it.
    if (this.s[this.pos] !== "\\" || this.s[this.pos + 1] !== "u") {
      this.error("invalid \\u escape");
    }
    const hex = this.s.slice(this.pos + 2, this.pos + 6);
    if (!/^[0-9a-fA-F]{4}$/.test(hex)) this.error("invalid \\u escape");
    this.pos += 6;
    return parseInt(hex, 16);
  }

  private parseArray(): JsonValue[] {
    this.pos++; // [
    const arr: JsonValue[] = [];
    this.skipWs();
    if (this.s[this.pos] === "]") {
      this.pos++;
      return arr;
    }
    for (;;) {
      arr.push(this.parseValue());
      this.skipWs();
      if (this.s[this.pos] === ",") {
        this.pos++;
        this.skipWs();
        if (this.s[this.pos] === "]") this.error("trailing comma in array");
        continue;
      }
      if (this.s[this.pos] === "]") {
        this.pos++;
        return arr;
      }
      this.error("expected ',' or ']'");
    }
  }

  private parseObject(): { [key: string]: JsonValue } {
    this.pos++; // {
    const obj: { [key: string]: JsonValue } = {};
    this.skipWs();
    if (this.s[this.pos] === "}") {
      this.pos++;
      return obj;
    }
    for (;;) {
      this.skipWs();
      if (this.s[this.pos] !== '"') this.error("object key must be a string");
      const key = this.parseString();
      if (Object.prototype.hasOwnProperty.call(obj, key)) {
        this.error(`duplicate key '${key}' in object`);
      }
      this.skipWs();
      if (this.s[this.pos] !== ":") this.error("expected ':' after object key");
      this.pos++;
      const value = this.parseValue();
      obj[key] = value;
      this.skipWs();
      if (this.s[this.pos] === ",") {
        this.pos++;
        this.skipWs();
        if (this.s[this.pos] === "}") this.error("trailing comma in object");
        continue;
      }
      if (this.s[this.pos] === "}") {
        this.pos++;
        return obj;
      }
      this.error("expected ',' or '}'");
    }
  }
}

function isDigit(code: number): boolean {
  return code >= 0x30 && code <= 0x39;
}
function isDigit1to9(code: number): boolean {
  return code >= 0x31 && code <= 0x39;
}

/** Parse a complete JSON document (must be exactly one value). */
export function parseStrictJson(text: string): JsonValue {
  const sc = new Scanner(text);
  const value = sc.parseValue();
  sc.skipWs();
  if (sc.pos !== text.length) {
    throw new SyntaxError(`JSON parse error at offset ${sc.pos}: trailing characters after document`);
  }
  return value;
}
