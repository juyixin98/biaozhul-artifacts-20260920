import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseQuery } from '../src/query/parser.js';
import { validateQuery } from '../src/query/validator.js';
import { appSchema } from '../src/fixtures/schemaDef.js';

const schema = appSchema();
const RUN = 'unit-contract';

test('parser: 接受别名、参数与变量引用并结构化为 AST', () => {
  // Arrange
  const text = 'query ($uid: INT!) { userById(id: $uid) { id name: name } }';
  // Act
  const q = parseQuery(text, RUN);
  // Assert
  assert.equal(q.variables[0]?.name, 'uid');
  assert.equal(q.variables[0]?.declaredType, 'INT');
  const root = q.selections[0];
  assert.equal(root?.kind, 'field');
  if (root?.kind !== 'field') throw new Error('bad ast');
  assert.equal(root.name, 'userById');
  assert.deepEqual(root.args.id, { __varRef: true, name: 'uid' });
  assert.equal(root.selections[1]?.kind === 'field' ? root.selections[1].alias : '', 'name');
});

test('parser: 语法错误归类 INPUT_INVALID（字符多少不影响分类）', () => {
  assert.throws(
    () => parseQuery('{ users { ', RUN),
    (err: { category?: string }) => err.category === 'INPUT_INVALID',
  );
});

test('validator: 变量先做类型校验 —— INT 变量收到字符串直接 INPUT_INVALID', () => {
  // Arrange
  const q = parseQuery('query ($uid: INT!) { userById(id: $uid) { id } }', RUN);
  // Act / Assert
  assert.throws(
    () => validateQuery(schema, q, { uid: 'abc' }, RUN),
    (err: { category?: string; message?: string }) =>
      err.category === 'INPUT_INVALID' && /expected INT/.test(err.message ?? ''),
  );
});

test('validator: BOOL 变量收到数字拒绝；合法 INT 变量放行', () => {
  const bad = parseQuery('query ($f: BOOL) { users { id } }', RUN);
  assert.throws(
    () => validateQuery(schema, bad, { f: 1 }, RUN),
    (e: { category?: string }) => e.category === 'INPUT_INVALID',
  );

  const good = parseQuery('query ($uid: INT = 7) { userById(id: $uid) { id } }', RUN);
  const plan = validateQuery(schema, good, {}, RUN);
  assert.equal(plan.variables.uid?.value, 7);
});

test('validator: 未声明的片段 → INPUT_INVALID', () => {
  const q = parseQuery('{ users { ...Nope } }', RUN);
  assert.throws(
    () => validateQuery(schema, q, {}, RUN),
    (e: { category?: string }) => e.category === 'INPUT_INVALID',
  );
});

test('validator: 循环片段 ...LoopA→...LoopB→...LoopA 判定 STATE_CONFLICT 且带 cycle', () => {
  // Arrange
  const q = parseQuery('{ users { ...LoopA } }', RUN);
  try {
    // Act
    validateQuery(schema, q, {}, RUN);
    assert.fail('expected cyclic fragment rejection');
  } catch (err) {
    // Assert
    const e = err as { category: string; context: { cycle?: string[] } };
    assert.equal(e.category, 'STATE_CONFLICT');
    assert.deepEqual(e.context.cycle, ['LoopA', 'LoopB', 'LoopA']);
  }
});

test('validator: 同一别名绑定不同字段 → STATE_CONFLICT；同字段重复选择允许', () => {
  const conflict = parseQuery('{ users { x: id x: name } }', RUN);
  assert.throws(
    () => validateQuery(schema, conflict, {}, RUN),
    (e: { category?: string }) => e.category === 'STATE_CONFLICT',
  );

  const sameFieldTwice = parseQuery('{ users { id id } }', RUN);
  assert.doesNotThrow(() => validateQuery(schema, sameFieldTwice, {}, RUN));
});

test('validator: 标量字段带选择集 / 对象字段缺选择集均为 INPUT_INVALID', () => {
  assert.throws(
    () => validateQuery(schema, parseQuery('{ users { id { x } } }', RUN), {}, RUN),
    (e: { category?: string }) => e.category === 'INPUT_INVALID',
  );
  assert.throws(
    () => validateQuery(schema, parseQuery('{ users { posts } }', RUN), {}, RUN),
    (e: { category?: string }) => e.category === 'INPUT_INVALID',
  );
});

test('validator: 引用未声明变量的参数 → INPUT_INVALID', () => {
  const q = parseQuery('{ userById(id: $missing) { id } }', RUN);
  assert.throws(
    () => validateQuery(schema, q, {}, RUN),
    (e: { category?: string }) => e.category === 'INPUT_INVALID',
  );
});
