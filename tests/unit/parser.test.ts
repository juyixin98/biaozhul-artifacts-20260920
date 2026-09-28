/**
 * 词法/语法单元测试：断言 AST 结构与语法错误位置，而非"能解析即可"。
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { LexerError } from '../../src/graphql/lexer.js';
import { parse, ParseError } from '../../src/graphql/parser.js';

test('parser: 解析带别名、参数、变量定义与片段的文档', () => {
  const doc = parse(`
    query GetUser($id: ID!, $withFriends: Boolean = false) {
      user(id: $id) {
        id
        displayName
        ...FriendsFields
      }
    }
    fragment FriendsFields on User {
      friends @include(if: $withFriends) { handle }
    }
  `);

  assert.equal(doc.definitions.length, 2);
  const op = doc.definitions[0];
  assert.ok(op && op.kind === 'OperationDefinition');
  if (op.kind !== 'OperationDefinition') throw new Error('narrowed');
  assert.equal(op.operation, 'query');
  assert.equal(op.variableDefinitions.length, 2);
  assert.equal(op.variableDefinitions[0]?.variable.name.value, 'id');
  assert.equal(op.variableDefinitions[0]?.type.kind, 'NonNullType');
  assert.equal(op.variableDefinitions[1]?.defaultValue?.kind, 'BooleanValue');

  const fragment = doc.definitions[1];
  assert.ok(fragment && fragment.kind === 'FragmentDefinition');
  if (fragment.kind !== 'FragmentDefinition') throw new Error('narrowed');
  assert.equal(fragment.typeCondition.value, 'User');
  const spread = op.selectionSet.selections[0];
  assert.ok(spread && spread.kind === 'Field' && spread.selectionSet);
  if (spread.kind !== 'Field' || !spread.selectionSet) throw new Error('narrowed');
  assert.equal(spread.arguments[0]?.value.kind, 'Variable');
  assert.ok(
    spread.selectionSet.selections.some(
      (s) => s.kind === 'FragmentSpread' && s.name.value === 'FriendsFields',
    ),
  );
});

test('parser: 逗号被视为空白，列表值可以逗号分隔', () => {
  const doc = parse('{ users(ids: ["u-01", "u-02"]) { id, handle, } }');
  const root = doc.definitions[0];
  assert.ok(root && root.kind === 'OperationDefinition');
  if (root.kind !== 'OperationDefinition') throw new Error('narrowed');
  const field = root.selectionSet.selections[0];
  assert.ok(field && field.kind === 'Field');
  if (field.kind !== 'Field') throw new Error('narrowed');
  const idsArg = field.arguments[0];
  assert.ok(idsArg && idsArg.value.kind === 'ListValue');
  if (idsArg.value.kind !== 'ListValue') throw new Error('narrowed');
  assert.equal(idsArg.value.values.length, 2);
});

test('parser: 未终止字符串报 LexerError（读到行尾/EOF）', () => {
  assert.throws(
    () => parse('{ user(handle: "ada) { id } }'),
    (err: unknown) => err instanceof LexerError && /Unterminated string literal/.test((err as Error).message),
  );
});

test('parser: 未闭合选择集报 ParseError', () => {
  assert.throws(
    () => parse('query { user(id: "x") { id '),
    (err: unknown) =>
      err instanceof ParseError && /Unterminated selection set/.test((err as Error).message),
  );
});

test('parser: 片段名不允许使用 on', () => {
  assert.throws(
    () => parse('fragment on on User { id }'),
    (err: unknown) => err instanceof ParseError && /cannot be "on"/.test((err as Error).message),
  );
});

test('parser: 嵌套非空列表类型被正确还原', () => {
  const doc = parse('query Q($g: [[String!]!]!) { grid }');
  const op = doc.definitions[0];
  assert.ok(op && op.kind === 'OperationDefinition');
  if (op.kind !== 'OperationDefinition') throw new Error('narrowed');
  const t = op.variableDefinitions[0]?.type;
  assert.ok(t && t.kind === 'NonNullType');
  if (t.kind !== 'NonNullType') throw new Error('narrowed');
  assert.equal(t.type.kind, 'ListType');
  if (t.type.kind !== 'ListType') throw new Error('narrowed');
  assert.equal(t.type.type.kind, 'NonNullType');
});
