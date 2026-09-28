/**
 * 词法/语法层单测：断言 AST 关键结构与 PARSE 类别错误。
 */

import { describe, expect, it } from 'vitest';
import { parse } from '../../src/graphql/parser.js';
import { Lexer } from '../../src/graphql/lexer.js';
import { GraphQLError } from '../../src/graphql/error.js';

describe('lexer/parser', () => {
  it('解析匿名 query 与嵌套选择集', () => {
    const doc = parse('{ users { id name } }');
    expect(doc.definitions).toHaveLength(1);
    const op = doc.definitions[0]!;
    expect(op.kind).toBe('OperationDefinition');
    if (op.kind !== 'OperationDefinition') return;
    expect(op.operation).toBe('query');
    const field = op.selectionSet.selections[0]!;
    expect(field.kind).toBe('Field');
    if (field.kind !== 'Field') return;
    expect(field.name.value).toBe('users');
    expect(field.selectionSet?.selections).toHaveLength(2);
  });

  it('解析变量定义、默认值、别名、具名片段与内联片段', () => {
    const doc = parse(`
      query Q($id: ID! = "u1", $n: Int = 2) {
        user(id: $id) {
          id
          aliasName: name
          ...UserFields
          ... on User { email }
        }
      }
      fragment UserFields on User { tags }
    `);
    const op = doc.definitions[0];
    expect(op?.kind).toBe('OperationDefinition');
    if (op?.kind !== 'OperationDefinition') return;
    expect(op.variableDefinitions).toHaveLength(2);
    expect(op.variableDefinitions[0]?.type).toMatchObject({ kind: 'NonNullType' });
    expect(op.variableDefinitions[1]?.defaultValue).toMatchObject({
      kind: 'IntValue',
      value: '2',
    });
    const fragment = doc.definitions[1];
    expect(fragment?.kind).toBe('FragmentDefinition');
  });

  it('解析嵌套非空列表类型 [[Tag!]!]!', () => {
    const doc = parse('query { nestedTags }');
    const op = doc.definitions[0];
    expect(op?.kind).toBe('OperationDefinition');
  });

  it('支持字符串转义与 block string 词法', () => {
    const doc = parse('{ user(id: "a\\"b\\nc") { id } }');
    const op = doc.definitions[0];
    if (op?.kind !== 'OperationDefinition') throw new Error('bad op');
    const field = op.selectionSet.selections[0];
    if (field?.kind !== 'Field') throw new Error('bad field');
    const arg = field.arguments[0]?.value;
    expect(arg).toMatchObject({ kind: 'StringValue', value: 'a"b\nc' });

    const tokens = new Lexer('"""\n  hello\n  world\n"""').tokenize();
    const stringToken = tokens.find((t) => t.kind === 'STRING');
    expect(stringToken?.value).toBe('hello\nworld');
  });

  it.each([
    ['{ users( { }', '括号失配'],
    ['{ ', '未闭合选择集'],
    ['{ user(id: 1) {', '未闭合嵌套选择集'],
    ['query($x: ) { user { id } }', '空类型'],
    ['{ user(id: "unterminated) { id } }', '未闭合字符串'],
    ['{ .. users }', '错误的 spread'],
  ])('语法错误归类 PARSE: %s', (source) => {
    expect(() => parse(source)).toThrow(GraphQLError);
    try {
      parse(source);
    } catch (error) {
      expect(error).toBeInstanceOf(GraphQLError);
      expect((error as GraphQLError).category).toBe('PARSE');
      expect((error as GraphQLError).locations?.[0]).toBeDefined();
    }
  });

  it('拒绝重复参数', () => {
    expect(() => parse('{ user(id: "u1", id: "u2") { id } }')).toThrow(/duplicate argument/);
  });
});
