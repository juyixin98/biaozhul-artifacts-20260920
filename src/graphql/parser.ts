/**
 * GraphQL 受限子集递归下降解析器。
 * 产出 ast.ts 中定义的 DocumentNode。
 * 设计要点：
 * - 逗号视为空白（词法层已跳过），因此列表/参数中可写逗号也可不写；
 * - 仅支持 query / mutation 两种操作；
 * - 选择集合中的 ... 后跟 Name 为片段展开，跟 on/`{` 为内联片段。
 */
import {
  type ArgumentNode,
  type DefinitionNode,
  type DirectiveNode,
  type DocumentNode,
  type FieldNode,
  type FragmentDefinitionNode,
  type InlineFragmentNode,
  type ListTypeNode,
  type Location,
  type NamedTypeNode,
  type NameNode,
  type NonNullTypeNode,
  type ObjectFieldNode,
  type OperationDefinitionNode,
  type SelectionNode,
  type SelectionSetNode,
  type TypeNode,
  type ValueNode,
  type VariableDefinitionNode,
} from './ast.js';
import { Lexer, Token, TokenKind } from './lexer.js';

export class ParseError extends Error {
  constructor(
    message: string,
    readonly loc: Location,
  ) {
    super(`Syntax Error: ${message} (line ${loc.line}, column ${loc.column})`);
    this.name = 'ParseError';
  }
}

const PUNCT = TokenKind.PUNCT;

export class Parser {
  private tokens: Token[] = [];
  private index = 0;

  constructor(private readonly source: string) {}

  parse(): DocumentNode {
    const lexer = new Lexer(this.source);
    for (;;) {
      const token = lexer.next();
      this.tokens.push(token);
      if (token.kind === TokenKind.EOF) break;
    }

    const definitions: DefinitionNode[] = [];
    while (!this.atEnd()) {
      definitions.push(this.parseDefinition());
    }
    return { kind: 'Document', definitions };
  }

  // ---- 基础 token 工具 ----

  private current(): Token {
    return this.tokens[this.index]!;
  }

  private atEnd(): boolean {
    return this.current().kind === TokenKind.EOF;
  }

  private advance(): Token {
    const token = this.current();
    this.index += 1;
    return token;
  }

  private expectPunct(value: string): Token {
    const token = this.current();
    if (token.kind !== PUNCT || token.value !== value) {
      throw new ParseError(`Expected "${value}" but found "${token.value}"`, token.loc);
    }
    return this.advance();
  }

  private expectName(): Token {
    const token = this.current();
    if (token.kind !== TokenKind.NAME) {
      throw new ParseError(`Expected a name but found "${token.value}"`, token.loc);
    }
    return this.advance();
  }

  private isPunct(value: string): boolean {
    const token = this.current();
    return token.kind === PUNCT && token.value === value;
  }

  private isName(value?: string): boolean {
    const token = this.current();
    return token.kind === TokenKind.NAME && (value === undefined || token.value === value);
  }

  private consumeName(value: string): boolean {
    if (this.isName(value)) {
      this.advance();
      return true;
    }
    return false;
  }

  private nameNode(token?: Token): NameNode {
    const t = token ?? this.expectName();
    return { kind: 'Name', value: t.value, loc: t.loc };
  }

  // ---- 文档与定义 ----

  private parseDefinition(): DefinitionNode {
    if (this.isPunct('{')) {
      return this.parseOperationDefinition('query', null);
    }
    if (this.isName('query') || this.isName('mutation')) {
      const operationToken = this.advance();
      const operation = operationToken.value as 'query' | 'mutation';
      const name = this.isName() ? this.nameNode() : null;
      return this.parseOperationDefinition(operation, name);
    }
    if (this.isName('fragment')) {
      return this.parseFragmentDefinition();
    }
    const token = this.current();
    throw new ParseError(
      `Expected "query", "mutation", "fragment" or "{" but found "${token.value}"`,
      token.loc,
    );
  }

  private parseOperationDefinition(
    operation: 'query' | 'mutation',
    name: NameNode | null,
  ): OperationDefinitionNode {
    const loc = this.tokens[Math.max(0, this.index - 1)]!.loc;
    const variableDefinitions = this.isPunct('(') ? this.parseVariableDefinitions() : [];
    const directives = this.parseDirectivesOptional();
    const selectionSet = this.parseSelectionSet();
    return {
      kind: 'OperationDefinition',
      operation,
      name,
      variableDefinitions,
      directives,
      selectionSet,
      loc,
    };
  }

  private parseFragmentDefinition(): FragmentDefinitionNode {
    this.expectName(); // fragment
    const nameToken = this.expectName();
    if (nameToken.value === 'on') {
      throw new ParseError('Fragment name cannot be "on"', nameToken.loc);
    }
    if (!this.consumeName('on')) {
      throw new ParseError('Expected "on" in fragment definition', this.current().loc);
    }
    const typeCondition = this.nameNode();
    const directives = this.parseDirectivesOptional();
    const selectionSet = this.parseSelectionSet();
    return {
      kind: 'FragmentDefinition',
      name: { kind: 'Name', value: nameToken.value, loc: nameToken.loc },
      typeCondition,
      directives,
      selectionSet,
    };
  }

  // ---- 变量定义与类型 ----

  private parseVariableDefinitions(): VariableDefinitionNode[] {
    this.expectPunct('(');
    const defs: VariableDefinitionNode[] = [];
    while (!this.isPunct(')')) {
      if (this.atEnd()) throw new ParseError('Unterminated variable definitions', this.current().loc);
      const variableToken = this.expectPunct('$');
      const variable = {
        kind: 'Variable' as const,
        name: this.nameNode(),
        loc: variableToken.loc,
      };
      this.expectPunct(':');
      const type = this.parseType();
      const defaultValue = this.consumeEquals() ? this.parseValue(true) : null;
      const directives = this.parseDirectivesOptional();
      defs.push({ kind: 'VariableDefinition', variable, type, defaultValue, directives });
    }
    this.expectPunct(')');
    return defs;
  }

  /** consumeName 返回布尔，这里需要"是否消费了 ="，单独封装以免误用 */
  private consumeEquals(): boolean {
    if (this.isPunct('=')) {
      this.advance();
      return true;
    }
    return false;
  }

  private parseType(): TypeNode {
    let type: TypeNode;
    if (this.isPunct('[')) {
      const loc = this.advance().loc;
      const inner = this.parseType();
      this.expectPunct(']');
      const list: ListTypeNode = { kind: 'ListType', type: inner, loc };
      type = list;
    } else {
      const nameToken = this.expectName();
      const named: NamedTypeNode = {
        kind: 'NamedType',
        name: { kind: 'Name', value: nameToken.value, loc: nameToken.loc },
        loc: nameToken.loc,
      };
      type = named;
    }
    if (this.isPunct('!')) {
      const loc = this.advance().loc;
      const nonNull: NonNullTypeNode = { kind: 'NonNullType', type, loc };
      return nonNull;
    }
    return type;
  }

  // ---- 选择集 ----

  private parseSelectionSet(): SelectionSetNode {
    this.expectPunct('{');
    const selections: SelectionNode[] = [];
    while (!this.isPunct('}')) {
      if (this.atEnd()) throw new ParseError('Unterminated selection set', this.current().loc);
      selections.push(this.parseSelection());
    }
    this.expectPunct('}');
    return { kind: 'SelectionSet', selections };
  }

  private parseSelection(): SelectionNode {
    if (this.isPunct('...')) {
      const spreadLoc = this.advance().loc;
      if (this.isName('on')) {
        this.advance();
        const typeCondition = this.nameNode();
        const directives = this.parseDirectivesOptional();
        const selectionSet = this.parseSelectionSet();
        const inline: InlineFragmentNode = {
          kind: 'InlineFragment',
          typeCondition,
          directives,
          selectionSet,
        };
        return inline;
      }
      if (this.isPunct('{') || this.isPunct('@')) {
        const directives = this.parseDirectivesOptional();
        const selectionSet = this.parseSelectionSet();
        return {
          kind: 'InlineFragment',
          typeCondition: null,
          directives,
          selectionSet,
        };
      }
      // ...spreadLoc 被消费但未直接使用，保留在变量上以便调试断点
      void spreadLoc;
      const name = this.nameNode();
      const directives = this.parseDirectivesOptional();
      return { kind: 'FragmentSpread', name, directives };
    }
    return this.parseField();
  }

  private parseField(): FieldNode {
    const first = this.expectName();
    let alias: NameNode | null = null;
    let name: NameNode = { kind: 'Name', value: first.value, loc: first.loc };
    if (this.isPunct(':')) {
      this.advance();
      alias = name;
      name = this.nameNode();
    }
    const args = this.isPunct('(') ? this.parseArguments() : [];
    const directives = this.parseDirectivesOptional();
    const selectionSet = this.isPunct('{') ? this.parseSelectionSet() : null;
    return {
      kind: 'Field',
      name,
      alias,
      arguments: args,
      directives,
      selectionSet,
    };
  }

  // ---- 参数与指令 ----

  private parseArguments(): ArgumentNode[] {
    this.expectPunct('(');
    const args: ArgumentNode[] = [];
    const seen = new Set<string>();
    while (!this.isPunct(')')) {
      if (this.atEnd()) throw new ParseError('Unterminated arguments', this.current().loc);
      const nameToken = this.expectName();
      this.expectPunct(':');
      const value = this.parseValue(false);
      if (seen.has(nameToken.value)) {
        throw new ParseError(`Duplicate argument "${nameToken.value}"`, nameToken.loc);
      }
      seen.add(nameToken.value);
      args.push({
        kind: 'Argument',
        name: { kind: 'Name', value: nameToken.value, loc: nameToken.loc },
        value,
      });
    }
    this.expectPunct(')');
    return args;
  }

  private parseDirectivesOptional(): DirectiveNode[] {
    const directives: DirectiveNode[] = [];
    const seen = new Set<string>();
    while (this.isPunct('@')) {
      const atLoc = this.advance().loc;
      const nameToken = this.expectName();
      if (seen.has(nameToken.value)) {
        throw new ParseError(`Duplicate directive "@${nameToken.value}"`, atLoc);
      }
      seen.add(nameToken.value);
      const args = this.isPunct('(') ? this.parseArguments() : [];
      directives.push({
        kind: 'Directive',
        name: { kind: 'Name', value: nameToken.value, loc: nameToken.loc },
        arguments: args,
      });
    }
    return directives;
  }

  // ---- 值 ----

  private parseValue(constant: boolean): ValueNode {
    const token = this.current();

    if (token.kind === PUNCT && token.value === '$') {
      if (constant) {
        throw new ParseError('Variable references are not allowed in default values', token.loc);
      }
      this.advance();
      const name = this.nameNode();
      return { kind: 'Variable', name, loc: token.loc };
    }

    if (token.kind === TokenKind.INT) {
      this.advance();
      return { kind: 'IntValue', value: Number.parseInt(token.value, 10), loc: token.loc };
    }

    if (token.kind === TokenKind.STRING) {
      this.advance();
      return { kind: 'StringValue', value: token.value, loc: token.loc };
    }

    if (token.kind === TokenKind.NAME) {
      this.advance();
      if (token.value === 'true' || token.value === 'false') {
        return { kind: 'BooleanValue', value: token.value === 'true', loc: token.loc };
      }
      if (token.value === 'null') {
        return { kind: 'NullValue', loc: token.loc };
      }
      return { kind: 'EnumValue', value: token.value, loc: token.loc };
    }

    if (token.kind === PUNCT && token.value === '[') {
      this.advance();
      const values: ValueNode[] = [];
      while (!this.isPunct(']')) {
        if (this.atEnd()) throw new ParseError('Unterminated list value', this.current().loc);
        values.push(this.parseValue(constant));
      }
      this.expectPunct(']');
      return { kind: 'ListValue', values, loc: token.loc };
    }

    if (token.kind === PUNCT && token.value === '{') {
      this.advance();
      const fields: ObjectFieldNode[] = [];
      const seen = new Set<string>();
      while (!this.isPunct('}')) {
        if (this.atEnd()) throw new ParseError('Unterminated object value', this.current().loc);
        const fieldNameToken = this.expectName();
        this.expectPunct(':');
        const value = this.parseValue(constant);
        if (seen.has(fieldNameToken.value)) {
          throw new ParseError(`Duplicate object field "${fieldNameToken.value}"`, fieldNameToken.loc);
        }
        seen.add(fieldNameToken.value);
        fields.push({
          kind: 'ObjectField',
          name: { kind: 'Name', value: fieldNameToken.value, loc: fieldNameToken.loc },
          value,
        });
      }
      this.expectPunct('}');
      return { kind: 'ObjectValue', fields, loc: token.loc };
    }

    throw new ParseError(`Unexpected token "${token.value}" while parsing value`, token.loc);
  }
}

export function parse(source: string): DocumentNode {
  return new Parser(source).parse();
}
