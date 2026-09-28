/**
 * 变量强制与标量输出强制单元测试：边界值与失败类别。
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { coerceOutputScalar, coerceVariable, parseTypeRef } from '../../src/graphql/index.js';

const lookup = (): undefined => undefined;

test('coerceVariable: 非空标量收到 null 抛 VARIABLE_TYPE', () => {
  assert.throws(
    () => coerceVariable(null, parseTypeRef('String!'), lookup),
    /non-null type "String!"/,
  );
});

test('coerceVariable: 可空标量收到 null 返回 null', () => {
  assert.equal(coerceVariable(null, parseTypeRef('String'), lookup), null);
});

test('coerceVariable: 非空列表中 null 元素抛错且路径含索引', () => {
  assert.throws(
    () => coerceVariable(['a', null], parseTypeRef('[String!]!'), lookup),
    (err: unknown) => {
      assert.equal((err as { category?: string }).category ?? '', 'VARIABLE_TYPE');
      return /index 1/.test((err as Error).message);
    },
  );
});

test('coerceVariable: 非数组单值按规范包装为单元素列表', () => {
  assert.deepEqual(coerceVariable('x', parseTypeRef('[String!]'), lookup), ['x']);
});

test('coerceVariable: 嵌套列表逐层强制', () => {
  assert.deepEqual(coerceVariable([['a'], ['b', 'c']], parseTypeRef('[[String!]!]!'), lookup), [
    ['a'],
    ['b', 'c'],
  ]);
});

test('coerceVariable: Int 拒绝浮点、字符串与越界整数', () => {
  assert.throws(() => coerceVariable(1.5, parseTypeRef('Int!'), lookup));
  assert.throws(() => coerceVariable('1', parseTypeRef('Int!'), lookup));
  assert.throws(() => coerceVariable(Number.MAX_SAFE_INTEGER + 1, parseTypeRef('Int!'), lookup));
});

test('coerceVariable: ID 接受字符串与安全整数', () => {
  assert.equal(coerceVariable('u-1', parseTypeRef('ID!'), lookup), 'u-1');
  assert.equal(coerceVariable(42, parseTypeRef('ID!'), lookup), '42');
});

test('coerceOutputScalar: Int 输出非法值抛 COERCION_FAILURE', () => {
  assert.throws(
    () => coerceOutputScalar('nope', 'Int'),
    (err: unknown) => (err as { category?: string }).category === 'COERCION_FAILURE',
  );
});

test('coerceOutputScalar: Boolean 输出非法值抛 COERCION_FAILURE', () => {
  assert.throws(() => coerceOutputScalar(1, 'Boolean'));
});
