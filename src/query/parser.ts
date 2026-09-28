/**
 * 查询文本解析器（递归下降）。
 * 仅负责把文本结构化为 ParsedQuery：
 *  - 不做类型校验（validator 负责）
 *  - 不计成本（字符数不是成本单位）
 * 语法错误归类 INPUT_INVALID。
 */
import type { FieldSelection, ParsedQuery, SelectionDef, VariableDef } from '../contract/types.js';
import { DomainError } from '../errors/DomainError.js';

/** 参数中的变量引用，与普通字面量区分 */
export interface VarRef {
  readonly __varRef: true;
  readonly name: string;
}

export function varRef(name: string): VarRef {
  return { __varRef: true, name };
}

export function isVarRef(v: unknown): v is VarRef {
  return typeof v === 'object' && v !== null && (v as { __varRef?: unknown }).__varRef === true;
}

type TokenType = 'IDENT' | 'STRING' | 'NUMBER' | 'PUNCT' | 'VAR';

interface Token {
  type: TokenType;
  value: string;
  pos: number;
}

const PUNCTUATORS = ['...', '{', '}', '(', ')', ':', ',', '=', '[', ']', '!'];

function tokenize(source: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  while (i < source.length) {
    const c = source[i];
    if (c === '#' ) {
      while (i < source.length && source[i] !== '\n') i++;
      continue;
    }
    if (/\s|,/.test(c)) { i++; continue; }
    if (c === '"' || c === "'") {
      const quote = c;
      const start = i++;
      let value = '';
      while (i < source.length && source[i] !== quote) {
        if (source[i] === '\\' && i + 1 < source.length) { value += source[i + 1]; i += 2; }
        else { value += source[i++]; }
      }
      if (source[i] !== quote) {
        throw lexError(`unterminated string at offset ${start}`);
      }
      i++;
      tokens.push({ type: 'STRING', value, pos: start });
      continue;
    }
    if (c === '$') {
      const start = i++;
      let name = '';
      while (i < source.length && /[A-Za-z0-9_]/.test(source[i])) name += source[i++];
      if (!name) throw lexError(`empty variable name at offset ${start}`);
      tokens.push({ type: 'VAR', value: name, pos: start });
      continue;
    }
    if (c === '-' || /[0-9]/.test(c)) {
      const start = i;
      if (c === '-') i++;
      while (i < source.length && /[0-9.]/.test(source[i])) i++;
      const raw = source.slice(start, i);
      if (!/^-?\d+(\.\d+)?$/.test(raw)) throw lexError(`bad number "${raw}" at offset ${start}`);
      tokens.push({ type: 'NUMBER', value: raw, pos: start });
      continue;
    }
    if (/[A-Za-z_]/.test(c)) {
      const start = i;
      let ident = '';
      while (i < source.length && /[A-Za-z0-9_]/.test(source[i])) ident += source[i++];
      tokens.push({ type: 'IDENT', value: ident, pos: start });
      continue;
    }
    const punct = PUNCTUATORS.find((p) => source.startsWith(p, i));
    if (punct) {
      tokens.push({ type: 'PUNCT', value: punct, pos: i });
      i += punct.length;
      continue;
    }
    throw lexError(`unexpected character "${c}" at offset ${i}`);
  }
  return tokens;

  function lexError(msg: string): DomainError {
    return new DomainError('INPUT_INVALID', `lex error: ${msg}`, 'pre-run');
  }
}

export function parseQuery(source: string, runId: string): ParsedQuery {
  const tokens = tokenize(source);
  const p = new Parser(tokens, source, runId);
  return p.parseDocument();
}

class Parser {
  private pos = 0;
  constructor(
    private readonly tokens: Token[],
    private readonly source: string,
    private readonly runId: string,
  ) {}

  private peek(): Token | undefined { return this.tokens[this.pos]; }
  private next(): Token {
    const t = this.tokens[this.pos++];
    if (!t) throw this.syntaxError('unexpected end of query');
    return t;
  }
  private acceptPunct(value: string): boolean {
    const t = this.peek();
    if (t && t.type === 'PUNCT' && t.value === value) { this.pos++; return true; }
    return false;
  }
  private expectPunct(value: string): Token {
    const t = this.next();
    if (t.type !== 'PUNCT' || t.value !== value) {
      throw this.syntaxError(`expected "${value}" but found "${t.value}"`, t.pos);
    }
    return t;
  }
  private expectIdent(): Token {
    const t = this.next();
    if (t.type !== 'IDENT') throw this.syntaxError(`expected name but found "${t.value}"`, t.pos);
    return t;
  }
  private syntaxError(message: string, pos?: number): DomainError {
    const at = pos ?? this.peek()?.pos ?? this.source.length;
    return new DomainError('INPUT_INVALID', `syntax error: ${message} (offset ${at})`, this.runId, {
      offset: at,
    });
  }

  parseDocument(): ParsedQuery {
    const first = this.peek();
    if (first?.type === 'IDENT' && first.value === 'query') this.next();
    // 可选查询名
    if (this.peek()?.type === 'IDENT') this.next();
    const variables = this.acceptPunct('(') ? this.parseVariableDefs() : [];
    const selections = this.parseSelectionSet();
    if (this.pos !== this.tokens.length) {
      const t = this.next();
      throw this.syntaxError(`trailing tokens after query: "${t.value}"`, t.pos);
    }
    return { source: this.source, variables, selections };
  }

  private parseVariableDefs(): VariableDef[] {
    const defs: VariableDef[] = [];
    const seen = new Set<string>();
    do {
      const varTok = this.next();
      if (varTok.type !== 'VAR') throw this.syntaxError('expected $variable in variable definitions', varTok.pos);
      if (seen.has(varTok.value)) {
        throw new DomainError(
          'STATE_CONFLICT',
          `duplicate variable $${varTok.value}`,
          this.runId,
          { variable: varTok.value },
        );
      }
      seen.add(varTok.value);
      this.expectPunct(':');
      const typeTok = this.expectIdent();
      // 接受可选的非空后缀 "!"（如 INT!）；本实现类型即可空与否不影响成本。
      this.acceptPunct('!');
      let defaultValue: unknown;
      if (this.acceptPunct('=')) defaultValue = this.parseLiteral();
      defs.push({ name: varTok.value, declaredType: typeTok.value, defaultValue });
    } while (this.acceptPunct(','));
    this.expectPunct(')');
    return defs;
  }

  private parseSelectionSet(): SelectionDef[] {
    this.expectPunct('{');
    const selections: SelectionDef[] = [];
    while (!this.acceptPunct('}')) {
      if (this.pos >= this.tokens.length) throw this.syntaxError('unterminated selection set');
      selections.push(this.parseSelection());
    }
    return selections;
  }

  private parseSelection(): SelectionDef {
    const t = this.next();
    if (t.type === 'PUNCT' && t.value === '...') {
      const nameTok = this.expectIdent();
      return { kind: 'fragmentSpread', name: nameTok.value };
    }
    if (t.type !== 'IDENT') throw this.syntaxError(`expected field but found "${t.value}"`, t.pos);
    let name = t.value;
    let alias = t.value;
    if (this.acceptPunct(':')) {
      alias = name;
      name = this.expectIdent().value;
    }
    const args = this.acceptPunct('(') ? this.parseArgs() : {};
    const childSelections = this.peek()?.type === 'PUNCT' && this.peek()?.value === '{'
      ? this.parseSelectionSet()
      : [];
    const field: FieldSelection = { kind: 'field', name, alias, args, selections: childSelections };
    return field;
  }

  private parseArgs(): Record<string, unknown> {
    const args: Record<string, unknown> = {};
    do {
      const nameTok = this.expectIdent();
      if (nameTok.value in args) {
        throw new DomainError(
          'STATE_CONFLICT',
          `duplicate argument ${nameTok.value}`,
          this.runId,
          { argument: nameTok.value },
        );
      }
      this.expectPunct(':');
      args[nameTok.value] = this.parseArgValue();
    } while (this.acceptPunct(','));
    this.expectPunct(')');
    return args;
  }

  private parseArgValue(): unknown {
    const t = this.next();
    if (t.type === 'STRING') return t.value;
    if (t.type === 'NUMBER') return t.value.includes('.') ? Number.parseFloat(t.value) : Number.parseInt(t.value, 10);
    if (t.type === 'VAR') return varRef(t.value);
    if (t.type === 'IDENT') {
      if (t.value === 'true') return true;
      if (t.value === 'false') return false;
      if (t.value === 'null') return null;
      throw this.syntaxError(`unexpected identifier "${t.value}" in argument position`, t.pos);
    }
    if (t.type === 'PUNCT' && t.value === '[') {
      const arr: unknown[] = [];
      while (!this.acceptPunct(']')) arr.push(this.parseArgValue());
      return arr;
    }
    throw this.syntaxError(`bad argument value "${t.value}"`, t.pos);
  }

  private parseLiteral(): unknown {
    const t = this.next();
    if (t.type === 'STRING') return t.value;
    if (t.type === 'NUMBER') return t.value.includes('.') ? Number.parseFloat(t.value) : Number.parseInt(t.value, 10);
    if (t.type === 'IDENT') {
      if (t.value === 'true') return true;
      if (t.value === 'false') return false;
      if (t.value === 'null') return null;
    }
    throw this.syntaxError(`bad default value "${t.value}"`, t.pos);
  }
}
