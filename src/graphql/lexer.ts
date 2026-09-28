/**
 * GraphQL 受限子集词法分析器。
 * 负责：忽略逗号/BOM/空白（逗号在 GraphQL 中是空白）、
 * 跳过注释、识别名称、标点、整数与字符串（含简单转义）。
 * 不负责：BlockString（本工程 schema/查询均不使用）、数字指数形式。
 */
import type { Location } from './ast.js';

export const TokenKind = {
  PUNCT: 'PUNCT',
  NAME: 'NAME',
  INT: 'INT',
  STRING: 'STRING',
  EOF: 'EOF',
} as const;

export type TokenKindType = (typeof TokenKind)[keyof typeof TokenKind];

export interface Token {
  kind: TokenKindType;
  value: string;
  loc: Location;
}

export class LexerError extends Error {
  constructor(
    message: string,
    readonly loc: Location,
  ) {
    super(`Syntax Error: ${message} (line ${loc.line}, column ${loc.column})`);
    this.name = 'LexerError';
  }
}

const PUNCTUATORS = new Set([
  '!',
  '$',
  '(',
  ')',
  '...',
  ':',
  '=',
  '@',
  '[',
  ']',
  '{',
  '|',
  '}',
  '&',
]);

const NAME_START = /[A-Za-z_]/;
const NAME_CONTINUE = /[A-Za-z0-9_]/;

export class Lexer {
  private pos = 0;
  private line = 1;
  private lineStart = 0;

  constructor(private readonly source: string) {}

  private location(): Location {
    return {
      line: this.line,
      column: this.pos - this.lineStart + 1,
      offset: this.pos,
    };
  }

  private peek(offset = 0): string {
    return this.source[this.pos + offset] ?? '';
  }

  private advance(): string {
    const ch = this.source[this.pos] ?? '';
    this.pos += 1;
    if (ch === '\n') {
      this.line += 1;
      this.lineStart = this.pos;
    }
    return ch;
  }

  private skipIgnored(): void {
    for (;;) {
      const ch = this.peek();
      if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r' || ch === ',' || ch === '﻿') {
        this.advance();
      } else if (ch === '#') {
        while (this.peek() !== '' && this.peek() !== '\n') {
          this.advance();
        }
      } else {
        return;
      }
    }
  }

  next(): Token {
    this.skipIgnored();
    const loc = this.location();
    const ch = this.peek();

    if (ch === '') {
      return { kind: TokenKind.EOF, value: '', loc };
    }

    if (ch === '.') {
      if (this.peek(1) === '.' && this.peek(2) === '.') {
        this.advance();
        this.advance();
        this.advance();
        return { kind: TokenKind.PUNCT, value: '...', loc };
      }
      throw new LexerError('Unexpected character "."', loc);
    }

    if (PUNCTUATORS.has(ch)) {
      this.advance();
      return { kind: TokenKind.PUNCT, value: ch, loc };
    }

    if (NAME_START.test(ch)) {
      let value = '';
      while (NAME_CONTINUE.test(this.peek())) {
        value += this.advance();
      }
      return { kind: TokenKind.NAME, value, loc };
    }

    if (ch === '-' || /[0-9]/.test(ch)) {
      let value = '';
      if (ch === '-') value += this.advance();
      while (/[0-9]/.test(this.peek())) value += this.advance();
      if (!/^-?[0-9]+$/.test(value)) {
        throw new LexerError(`Invalid number "${value}"`, loc);
      }
      return { kind: TokenKind.INT, value, loc };
    }

    if (ch === '"') {
      if (this.peek(1) === '"' && this.peek(2) === '"') {
        throw new LexerError('Block strings are not supported in this restricted subset', loc);
      }
      return { kind: TokenKind.STRING, value: this.readString(loc), loc };
    }

    throw new LexerError(`Unexpected character "${ch}"`, loc);
  }

  private readString(start: Location): string {
    this.advance(); // opening quote
    let value = '';
    for (;;) {
      const ch = this.peek();
      if (ch === '') {
        throw new LexerError('Unterminated string literal', start);
      }
      if (ch === '\n' || ch === '\r') {
        throw new LexerError('Unterminated string literal', this.location());
      }
      if (ch === '"') {
        this.advance();
        return value;
      }
      if (ch === '\\') {
        value += this.readEscape();
      } else {
        value += this.advance();
      }
    }
  }

  private readEscape(): string {
    const loc = this.location();
    this.advance(); // backslash
    const ch = this.advance();
    switch (ch) {
      case '"':
        return '"';
      case '\\':
        return '\\';
      case '/':
        return '/';
      case 'b':
        return '\b';
      case 'f':
        return '\f';
      case 'n':
        return '\n';
      case 'r':
        return '\r';
      case 't':
        return '\t';
      case 'u': {
        const hex = this.source.slice(this.pos, this.pos + 4);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) {
          throw new LexerError('Invalid unicode escape sequence', loc);
        }
        this.pos += 4;
        return String.fromCharCode(parseInt(hex, 16));
      }
      default:
        throw new LexerError(`Invalid escape sequence "\\${ch}"`, loc);
    }
  }
}
