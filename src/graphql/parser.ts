/**
 * GraphQL 递归下降解析器（受限子集）。
 * 产出带行列位置的 AST，错误类别 PARSE。
 */

import type {
  ArgumentNode,
  DefinitionNode,
  DirectiveNode,
  DocumentNode,
  FieldNode,
  FragmentDefinitionNode,
  FragmentSpreadNode,
  InlineFragmentNode,
  ListTypeNode,
  Location,
  NameNode,
  NamedTypeNode,
  NonNullTypeNode,
  ObjectFieldNode,
  OperationDefinitionNode,
  OperationTypeNode,
  SelectionNode,
  SelectionSetNode,
  TypeNode,
  ValueNode,
  VariableDefinitionNode,
  VariableNode,
} from './ast.js';
import { GraphQLError } from './error.js';
import { Lexer, type Token, type TokenKind } from './lexer.js';

export function parse(source: string): DocumentNode {
  return new Parser(source).parseDocument();
}

class Parser {
  private readonly tokens: Token[];
  private index = 0;

  constructor(source: string) {
    this.tokens = new Lexer(source).tokenize();
  }

  private get current(): Token {
    return this.tokens[this.index];
  }

  private locOf(token: Token): Location {
    return { line: token.line, column: token.column, offset: token.offset };
  }

  private peek(kind: TokenKind): boolean {
    return this.current.kind === kind;
  }

  private expect(kind: TokenKind, what: string): Token {
    const token = this.current;
    if (token.kind !== kind) {
      throw new GraphQLError(
        `Syntax Error: expected ${what}, found ${this.describe(token)} (line ${token.line}:${token.column})`,
        {
          category: 'PARSE',
          locations: [{ line: token.line, column: token.column }],
        },
      );
    }
    this.index += 1;
    return token;
  }

  private describe(token: Token): string {
    if (token.kind === 'EOF') return '<end of document>';
    return `"${token.value || token.kind}"`;
  }

  private expectName(): NameNode {
    const token = this.expect('NAME', 'a name');
    return { kind: 'Name', value: token.value, loc: this.locOf(token) };
  }

  private parseNameIf(): NameNode | null {
    if (this.peek('NAME')) {
      const token = this.current;
      this.index += 1;
      return { kind: 'Name', value: token.value, loc: this.locOf(token) };
    }
    return null;
  }

  parseDocument(): DocumentNode {
    const definitions: DefinitionNode[] = [];
    while (!this.peek('EOF')) {
      definitions.push(this.parseDefinition());
    }
    return { kind: 'Document', definitions };
  }

  private parseDefinition(): DefinitionNode {
    if (this.peek('LBRACE')) {
      return this.parseAnonymousQuery();
    }
    if (this.peek('NAME')) {
      const keyword = this.current.value;
      if (keyword === 'query' || keyword === 'mutation' || keyword === 'subscription') {
        return this.parseOperation(keyword as OperationTypeNode);
      }
      if (keyword === 'fragment') {
        return this.parseFragmentDefinition();
      }
    }
    const t = this.current;
    throw new GraphQLError(
      `Syntax Error: unexpected ${this.describe(t)}; expected an operation or fragment definition (line ${t.line}:${t.column})`,
      { category: 'PARSE', locations: [{ line: t.line, column: t.column }] },
    );
  }

  private parseAnonymousQuery(): OperationDefinitionNode {
    const first = this.current;
    const selectionSet = this.parseSelectionSet();
    return {
      kind: 'OperationDefinition',
      operation: 'query',
      name: null,
      variableDefinitions: [],
      directives: [],
      selectionSet,
      loc: this.locOf(first),
    };
  }

  private parseOperation(operation: OperationTypeNode): OperationDefinitionNode {
    const first = this.current;
    this.index += 1; // operation keyword
    const name = this.parseNameIf();
    const variableDefinitions = this.peek('LPAREN')
      ? this.parseVariableDefinitions()
      : [];
    const directives = this.parseDirectives();
    const selectionSet = this.parseSelectionSet();
    return {
      kind: 'OperationDefinition',
      operation,
      name,
      variableDefinitions,
      directives,
      selectionSet,
      loc: this.locOf(first),
    };
  }

  private parseVariableDefinitions(): VariableDefinitionNode[] {
    this.expect('LPAREN', '"("');
    const defs: VariableDefinitionNode[] = [];
    while (!this.peek('RPAREN')) {
      const first = this.current;
      const dollar = this.expect('DOLLAR', '"$"');
      const name = this.expectName();
      const variable: VariableNode = {
        kind: 'Variable',
        name,
        loc: this.locOf(dollar),
      };
      this.expect('COLON', '":"');
      const type = this.parseType();
      const defaultValue = this.peek('EQUALS')
        ? (this.index += 1, this.parseValueLiteral(true))
        : null;
      this.parseDirectives(); // 变量定义上的指令：接受并忽略
      defs.push({
        kind: 'VariableDefinition',
        variable,
        type,
        defaultValue,
        loc: this.locOf(first),
      });
    }
    this.expect('RPAREN', '")"');
    return defs;
  }

  private parseType(): TypeNode {
    let inner: NamedTypeNode | ListTypeNode;
    if (this.peek('LBRACKET')) {
      const first = this.current;
      this.index += 1;
      const elementType = this.parseType();
      this.expect('RBRACKET', '"]"');
      inner = { kind: 'ListType', type: elementType, loc: this.locOf(first) } satisfies ListTypeNode;
    } else {
      inner = this.parseNamedType();
    }
    if (this.peek('BANG')) {
      const bang = this.current;
      this.index += 1;
      return {
        kind: 'NonNullType',
        type: inner,
        loc: this.locOf(bang),
      } satisfies NonNullTypeNode;
    }
    return inner;
  }

  private parseNamedType(): NamedTypeNode {
    const first = this.current;
    const name = this.expectName();
    return { kind: 'NamedType', name, loc: this.locOf(first) };
  }

  private parseFragmentDefinition(): FragmentDefinitionNode {
    const first = this.current;
    this.index += 1; // fragment
    const name = this.expectName();
    if (name.value === 'on') {
      throw new GraphQLError('Syntax Error: fragment cannot be named "on"', {
        category: 'PARSE',
        locations: [{ line: name.loc.line, column: name.loc.column }],
      });
    }
    const on = this.expect('NAME', '"on"');
    if (on.value !== 'on') {
      throw new GraphQLError(
        `Syntax Error: expected "on", found "${on.value}" (line ${on.line}:${on.column})`,
        { category: 'PARSE', locations: [{ line: on.line, column: on.column }] },
      );
    }
    const typeCondition = this.parseNamedType();
    const directives = this.parseDirectives();
    const selectionSet = this.parseSelectionSet();
    return {
      kind: 'FragmentDefinition',
      name,
      typeCondition,
      directives,
      selectionSet,
      loc: this.locOf(first),
    };
  }

  private parseSelectionSet(): SelectionSetNode {
    const first = this.current;
    this.expect('LBRACE', '"{"');
    const selections: SelectionNode[] = [];
    while (!this.peek('RBRACE')) {
      if (this.peek('EOF')) {
        throw new GraphQLError('Syntax Error: unterminated selection set, expected "}"', {
          category: 'PARSE',
          locations: [{ line: first.line, column: first.column }],
        });
      }
      selections.push(this.parseSelection());
    }
    this.expect('RBRACE', '"}"');
    return { kind: 'SelectionSet', selections, loc: this.locOf(first) };
  }

  private parseSelection(): SelectionNode {
    if (this.peek('SPREAD')) {
      const spread = this.current;
      this.index += 1;
      // ...FragmentName  或  ...on T  或  ... { }
      if (this.peek('NAME') && this.current.value !== 'on') {
        const name = this.expectName();
        const directives = this.parseDirectives();
        return {
          kind: 'FragmentSpread',
          name,
          directives,
          loc: this.locOf(spread),
        } satisfies FragmentSpreadNode;
      }
      let typeCondition: NamedTypeNode | null = null;
      if (this.peek('NAME') && this.current.value === 'on') {
        this.index += 1;
        typeCondition = this.parseNamedType();
      }
      const directives = this.parseDirectives();
      const selectionSet = this.parseSelectionSet();
      return {
        kind: 'InlineFragment',
        typeCondition,
        directives,
        selectionSet,
        loc: this.locOf(spread),
      } satisfies InlineFragmentNode;
    }
    return this.parseField();
  }

  private parseField(): FieldNode {
    const first = this.current;
    const firstName = this.expectName();
    let alias: NameNode | null = null;
    let name = firstName;
    if (this.peek('COLON')) {
      this.index += 1;
      alias = firstName;
      name = this.expectName();
    }
    const args = this.peek('LPAREN') ? this.parseArguments() : [];
    const directives = this.parseDirectives();
    const selectionSet = this.peek('LBRACE') ? this.parseSelectionSet() : null;
    return {
      kind: 'Field',
      alias,
      name,
      arguments: args,
      directives,
      selectionSet,
      loc: this.locOf(first),
    };
  }

  private parseArguments(): ArgumentNode[] {
    this.expect('LPAREN', '"("');
    const args: ArgumentNode[] = [];
    const seen = new Set<string>();
    while (!this.peek('RPAREN')) {
      const first = this.current;
      const name = this.expectName();
      if (seen.has(name.value)) {
        throw new GraphQLError(
          `Syntax Error: duplicate argument "${name.value}" (line ${name.loc.line}:${name.loc.column})`,
          { category: 'PARSE', locations: [{ line: name.loc.line, column: name.loc.column }] },
        );
      }
      seen.add(name.value);
      this.expect('COLON', '":"');
      const value = this.parseValueLiteral(false);
      args.push({ kind: 'Argument', name, value, loc: this.locOf(first) });
    }
    this.expect('RPAREN', '")"');
    return args;
  }

  private parseDirectives(): DirectiveNode[] {
    const directives: DirectiveNode[] = [];
    const seen = new Set<string>();
    while (this.peek('AT')) {
      const first = this.current;
      this.index += 1;
      const name = this.expectName();
      if (seen.has(name.value)) {
        throw new GraphQLError(
          `Syntax Error: duplicate directive "@${name.value}" (line ${name.loc.line}:${name.loc.column})`,
          { category: 'PARSE', locations: [{ line: name.loc.line, column: name.loc.column }] },
        );
      }
      seen.add(name.value);
      const args = this.peek('LPAREN') ? this.parseArguments() : [];
      directives.push({ kind: 'Directive', name, arguments: args, loc: this.locOf(first) });
    }
    return directives;
  }

  /**
   * 解析值。isConst=true 时（默认值位置）禁止变量引用。
   */
  private parseValueLiteral(isConst: boolean): ValueNode {
    const token = this.current;
    switch (token.kind) {
      case 'DOLLAR': {
        if (isConst) {
          throw new GraphQLError('Syntax Error: variables are not allowed in default values', {
            category: 'PARSE',
            locations: [{ line: token.line, column: token.column }],
          });
        }
        this.index += 1;
        const name = this.expectName();
        return { kind: 'Variable', name, loc: this.locOf(token) };
      }
      case 'INT':
        this.index += 1;
        return { kind: 'IntValue', value: token.value, loc: this.locOf(token) };
      case 'FLOAT':
        this.index += 1;
        return { kind: 'FloatValue', value: token.value, loc: this.locOf(token) };
      case 'STRING':
        this.index += 1;
        return {
          kind: 'StringValue',
          value: token.value,
          block: token.value.includes('\n'),
          loc: this.locOf(token),
        };
      case 'NAME': {
        this.index += 1;
        if (token.value === 'true' || token.value === 'false') {
          return { kind: 'BooleanValue', value: token.value === 'true', loc: this.locOf(token) };
        }
        if (token.value === 'null') {
          return { kind: 'NullValue', loc: this.locOf(token) };
        }
        return { kind: 'EnumValue', value: token.value, loc: this.locOf(token) };
      }
      case 'LBRACKET': {
        this.index += 1;
        const values: ValueNode[] = [];
        while (!this.peek('RBRACKET')) {
          values.push(this.parseValueLiteral(isConst));
        }
        this.expect('RBRACKET', '"]"');
        return { kind: 'ListValue', values, loc: this.locOf(token) };
      }
      case 'LBRACE': {
        this.index += 1;
        const fields: ObjectFieldNode[] = [];
        const seen = new Set<string>();
        while (!this.peek('RBRACE')) {
          const fieldStart = this.current;
          const fieldName = this.expectName();
          if (seen.has(fieldName.value)) {
            throw new GraphQLError(
              `Syntax Error: duplicate input object field "${fieldName.value}"`,
              { category: 'PARSE', locations: [{ line: fieldName.loc.line, column: fieldName.loc.column }] },
            );
          }
          seen.add(fieldName.value);
          this.expect('COLON', '":"');
          const value = this.parseValueLiteral(isConst);
          fields.push({ kind: 'ObjectField', name: fieldName, value, loc: this.locOf(fieldStart) });
        }
        this.expect('RBRACE', '"}"');
        return { kind: 'ObjectValue', fields, loc: this.locOf(token) };
      }
      default:
        throw new GraphQLError(
          `Syntax Error: unexpected ${this.describe(token)} where a value was expected (line ${token.line}:${token.column})`,
          { category: 'PARSE', locations: [{ line: token.line, column: token.column }] },
        );
    }
  }
}
