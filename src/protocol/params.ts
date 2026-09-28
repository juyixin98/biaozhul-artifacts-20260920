/**
 * RFC 7230/6266-style header parameter parsing.
 *
 * A header value looks like:
 *   token; key="quoted value"; key2=token
 *
 * Explicit rules (covered by contract tests):
 *  - parameters are separated by `;`; semicolons inside quoted strings are
 *    literal and must not split the parameter;
 *  - quoted strings honour backslash quoted-pairs (\", \\, etc.);
 *  - the same parameter name appearing twice in ONE header is a
 *    DUPLICATE_HEADER input error — `filename` and `filename*` are distinct
 *    names and may coexist (the extended one wins at the caller);
 *  - whitespace around tokens/quoted strings is trimmed;
 *  - empty parameters (`;;`) and parameters without `=` are malformed.
 */

import { inputError } from './errors.js';

export interface ParsedHeaderValue {
  value: string;
  /** Insertion-ordered, lower-cased parameter map. */
  params: Map<string, string>;
}

class ParamScanner {
  private i = 0;

  constructor(
    private readonly s: string,
    private readonly headerName: string,
  ) {}

  /** Parse the leading value token. */
  readValue(): string {
    const start = this.i;
    while (this.i < this.s.length && this.s[this.i] !== ';') {
      this.i++;
    }
    const value = this.s.slice(start, this.i).trim();
    if (value.length === 0) {
      throw inputError('MALFORMED_HEADER', `empty ${this.headerName} value`);
    }
    return value;
  }

  hasMore(): boolean {
    return this.i < this.s.length;
  }

  /** Read one `; key=( token | quoted-string )` parameter. */
  readParam(): { key: string; value: string } {
    // current char is ';' (or hasMore() was misused)
    this.i++; // skip ';'
    this.skipSpaces();
    const keyStart = this.i;
    while (this.i < this.s.length) {
      const c = this.s[this.i]!;
      if (c === ';' || c === '=' || c === ' ' || c === '\t') break;
      this.i++;
    }
    const key = this.s.slice(keyStart, this.i).toLowerCase();
    if (key.length === 0) {
      throw inputError('MALFORMED_HEADER', `${this.headerName} has an empty parameter name`, {
        header: this.headerName,
      });
    }
    this.skipSpaces();
    if (this.s[this.i] !== '=') {
      throw inputError('MALFORMED_HEADER', `${this.headerName} parameter "${key}" is missing "="`, {
        header: this.headerName,
        param: key,
      });
    }
    this.i++; // skip '='
    this.skipSpaces();

    let value: string;
    if (this.s[this.i] === '"') {
      value = this.readQuoted();
    } else {
      value = this.readToken();
    }
    this.skipSpaces();
    if (this.s[this.i] !== undefined && this.s[this.i] !== ';') {
      throw inputError('MALFORMED_HEADER', `${this.headerName} parameter "${key}" has trailing garbage`, {
        header: this.headerName,
        param: key,
      });
    }
    return { key, value };
  }

  private readToken(): string {
    const start = this.i;
    while (this.i < this.s.length && this.s[this.i] !== ';') {
      const c = this.s[this.i]!;
      // token chars per RFC 7230 token (delimiters rejected explicitly)
      if (c === '"' || c === '\\' || c < ' ') {
        throw inputError('MALFORMED_HEADER', `unquoted ${this.headerName} parameter contains an illegal character`, {
          header: this.headerName,
          at: this.i,
        });
      }
      this.i++;
    }
    return this.s.slice(start, this.i).trimEnd();
  }

  private readQuoted(): string {
    this.i++; // opening quote
    let out = '';
    while (this.i < this.s.length) {
      const c = this.s[this.i]!;
      if (c === '\\') {
        const next = this.s[this.i + 1];
        if (next === undefined) {
          throw inputError('MALFORMED_HEADER', `${this.headerName} has a dangling escape in quoted string`);
        }
        // quoted-pair: backslash escapes any single char (incl. CR would be
        // rejected earlier by the line splitter)
        out += next;
        this.i += 2;
      } else if (c === '"') {
        this.i++; // closing quote
        return out;
      } else {
        out += c;
        this.i++;
      }
    }
    throw inputError('MALFORMED_HEADER', `${this.headerName} quoted string is never closed`);
  }

  private skipSpaces(): void {
    while (this.i < this.s.length && (this.s[this.i] === ' ' || this.s[this.i] === '\t')) {
      this.i++;
    }
  }
}

/**
 * Parse a `value; param=...; param="..."` header. Throws MultipartError
 * (INPUT_ERROR / MALFORMED_HEADER or DUPLICATE_HEADER).
 */
export function parseHeaderParameters(
  raw: string,
  headerName: string,
): ParsedHeaderValue {
  const scanner = new ParamScanner(raw, headerName);
  const value = scanner.readValue();
  const params = new Map<string, string>();
  while (scanner.hasMore()) {
    const { key, value: paramValue } = scanner.readParam();
    if (params.has(key)) {
      throw inputError(
        'DUPLICATE_HEADER',
        `${headerName} lists parameter "${key}" more than once`,
        { header: headerName, param: key },
      );
    }
    params.set(key, paramValue);
  }
  return { value: value.toLowerCase(), params };
}
