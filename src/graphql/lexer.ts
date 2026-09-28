/**
 * GraphQL 词法分析器（受限子集）。
 * 输出 token 流，忽略逗号(规范中逗号无意义)与注释。
 * 支持：Name, Int, Float, String, BlockString, 标点 ! $ ( ) ... : = @ [ ] { } | &
 */

import { GraphQLError } from './error.js';
import type { Location } from './ast.js';

export type TokenKind =
  | 'EOF'
  | 'BANG'
  | 'DOLLAR'
  | 'LPAREN'
  | 'RPAREN'
  | 'SPREAD'
  | 'COLON'
  | 'EQUALS'
  | 'AT'
  | 'LBRACKET'
  | 'RBRACKET'
  | 'LBRACE'
  | 'RBRACE'
  | 'PIPE'
  | 'AMP'
  | 'NAME'
  | 'INT'
  | 'FLOAT'
  | 'STRING'
  | 'BLOCK_STRING';

export interface Token {
  kind: TokenKind;
  value: string;
  line: number;
  column: number;
  offset: number;
}

const PUNCTUATION: Record<string, TokenKind> = {
  '!': 'BANG',
  $: 'DOLLAR',
  '(': 'LPAREN',
  ')': 'RPAREN',
  ':': 'COLON',
  '=': 'EQUALS',
  '@': 'AT',
  '[': 'LBRACKET',
  ']': 'RBRACKET',
  '{': 'LBRACE',
  '}': 'RBRACE',
  '|': 'PIPE',
  '&': 'AMP',
};

const NAME_START = /[_A-Za-z]/;
const NAME_CONTINUE = /[_A-Za-z0-9]/;

export class Lexer {
  private pos = 0;
  private line = 1;
  private lineStart = 0;
  private tokens: Token[] = [];

  constructor(private readonly source: string) {}

  tokenize(): Token[] {
    this.tokens = [];
    while (true) {
      const token = this.readToken();
      this.tokens.push(token);
      if (token.kind === 'EOF') break;
    }
    return this.tokens;
  }

  private location(offset: number): Location {
    return {
      line: this.line,
      column: offset - this.lineStart + 1,
      offset,
    };
  }

  private readToken(): Token {
    this.skipIgnored();
    if (this.pos >= this.source.length) {
      return this.makeToken('EOF', '', this.pos);
    }

    const code = this.source[this.pos];

    if (code === '.') {
      if (this.source.startsWith('...', this.pos)) {
        const t = this.makeToken('SPREAD', '...', this.pos);
        this.pos += 3;
        return t;
      }
      throw this.syntaxError(`Unexpected character "."`);
    }

    if (code === '"') {
      return this.source[this.pos + 1] === '"' &&
        this.source[this.pos + 2] === '"'
        ? this.readBlockString()
        : this.readString();
    }

    if (PUNCTUATION[code]) {
      const t = this.makeToken(PUNCTUATION[code], code, this.pos);
      this.pos += 1;
      return t;
    }

    if (code === '-' || this.isDigit(code)) {
      return this.readNumber();
    }

    if (NAME_START.test(code)) {
      return this.readName();
    }

    throw this.syntaxError(`Unexpected character "${code}"`);
  }

  private makeToken(kind: TokenKind, value: string, offset: number): Token {
    return {
      kind,
      value,
      line: this.line,
      column: offset - this.lineStart + 1,
      offset,
    };
  }

  private skipIgnored(): void {
    while (this.pos < this.source.length) {
      const code = this.source[this.pos];
      if (code === ' ' || code === '\t' || code === ',' || code === '\r') {
        this.pos += 1;
      } else if (code === '\n') {
        this.pos += 1;
        this.line += 1;
        this.lineStart = this.pos;
      } else if (code === '#') {
        while (
          this.pos < this.source.length &&
          this.source[this.pos] !== '\n'
        ) {
          this.pos += 1;
        }
      } else {
        break;
      }
    }
  }

  private isDigit(code: string): boolean {
    return code >= '0' && code <= '9';
  }

  private readName(): Token {
    const start = this.pos;
    this.pos += 1;
    while (
      this.pos < this.source.length &&
      NAME_CONTINUE.test(this.source[this.pos])
    ) {
      this.pos += 1;
    }
    return this.makeToken('NAME', this.source.slice(start, this.pos), start);
  }

  private readNumber(): Token {
    const start = this.pos;
    let isFloat = false;
    if (this.source[this.pos] === '-') this.pos += 1;

    if (this.source[this.pos] === '0') {
      this.pos += 1;
      if (this.isDigit(this.source[this.pos] ?? '')) {
        throw this.syntaxError('Invalid number: leading zeros are not allowed');
      }
    } else {
      this.readDigits();
    }

    if (this.source[this.pos] === '.') {
      isFloat = true;
      this.pos += 1;
      if (!this.isDigit(this.source[this.pos] ?? '')) {
        throw this.syntaxError('Invalid number: expected digits after decimal point');
      }
      this.readDigits();
    }

    if (this.source[this.pos] === 'e' || this.source[this.pos] === 'E') {
      isFloat = true;
      this.pos += 1;
      if (this.source[this.pos] === '+' || this.source[this.pos] === '-') {
        this.pos += 1;
      }
      if (!this.isDigit(this.source[this.pos] ?? '')) {
        throw this.syntaxError('Invalid number: expected digits in exponent');
      }
      this.readDigits();
    }

    return this.makeToken(
      isFloat ? 'FLOAT' : 'INT',
      this.source.slice(start, this.pos),
      start,
    );
  }

  private readDigits(): void {
    while (this.pos < this.source.length && this.isDigit(this.source[this.pos])) {
      this.pos += 1;
    }
  }

  private readString(): Token {
    const start = this.pos;
    this.pos += 1; // opening quote
    let value = '';
    while (this.pos < this.source.length) {
      const code = this.source[this.pos];
      if (code === '"') {
        this.pos += 1;
        return this.makeToken('STRING', value, start);
      }
      if (code === '\n' || code === '\r') {
        throw this.syntaxError('Unterminated string literal');
      }
      if (code === '\\') {
        value += this.readEscape();
      } else {
        value += code;
        this.pos += 1;
      }
    }
    throw this.syntaxError('Unterminated string literal');
  }

  private readEscape(): string {
    this.pos += 1; // backslash
    const code = this.source[this.pos];
    const simple: Record<string, string> = {
      '"': '"',
      '\\': '\\',
      '/': '/',
      b: '\b',
      f: '\f',
      n: '\n',
      r: '\r',
      t: '\t',
    };
    if (simple[code] !== undefined) {
      this.pos += 1;
      return simple[code];
    }
    if (code === 'u') {
      this.pos += 1;
      const hex = this.source.slice(this.pos, this.pos + 4);
      if (!/^[0-9a-fA-F]{4}$/.test(hex)) {
        throw this.syntaxError('Invalid unicode escape sequence');
      }
      this.pos += 4;
      return String.fromCharCode(parseInt(hex, 16));
    }
    throw this.syntaxError(`Invalid escape character "\\${code}"`);
  }

  private readBlockString(): Token {
    const start = this.pos;
    this.pos += 3;
    let raw = '';
    while (this.pos < this.source.length) {
      if (this.source.startsWith('"""', this.pos)) {
        this.pos += 3;
        return this.makeToken('STRING', this.dedentBlockString(raw), start);
      }
      if (this.source[this.pos] === '\\' &&
          this.source.startsWith('\\"""', this.pos)) {
        raw += '"""';
        this.pos += 4;
        continue;
      }
      const code = this.source[this.pos];
      if (code === '\n') {
        this.line += 1;
        this.lineStart = this.pos + 1;
      }
      raw += code;
      this.pos += 1;
    }
    throw this.syntaxError('Unterminated block string literal');
  }

  private dedentBlockString(raw: string): string {
    const lines = raw.split('\n');
    let commonIndent: number | null = null;
    for (const line of lines.slice(1)) {
      const indent = line.match(/^[ \t]*/)![0].length;
      if (indent < line.length && (commonIndent === null || indent < commonIndent)) {
        commonIndent = indent;
      }
    }
    const dedented = lines.map((line, i) =>
      i === 0 || commonIndent === null ? line : line.slice(commonIndent),
    );
    while (dedented.length > 0 && dedented[0].trim() === '') dedented.shift();
    while (dedented.length > 0 && dedented.at(-1)!.trim() === '') dedented.pop();
    return dedented.join('\n');
  }

  private syntaxError(message: string): GraphQLError {
    const loc = this.location(this.pos);
    return new GraphQLError(`Syntax Error: ${message} (line ${loc.line}:${loc.column})`, {
      category: 'PARSE',
      locations: [{ line: loc.line, column: loc.column }],
    });
  }
}
