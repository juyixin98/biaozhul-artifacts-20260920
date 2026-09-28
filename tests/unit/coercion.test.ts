/**
 * 变量/实参强制转换单测：执行前类型校验与路径化错误。
 */

import { describe, expect, it } from 'vitest';
import { parse } from '../../src/graphql/parser.js';
import { coerceVariableValues } from '../../src/graphql/values.js';
import type { OperationDefinitionNode } from '../../src/graphql/ast.js';
import { buildSchema } from '../../src/state/schema.js';

function defsOf(source: string) {
  const doc = parse(source);
  const op = doc.definitions[0];
  if (op?.kind !== 'OperationDefinition') throw new Error('need operation');
  return op.variableDefinitions;
}

describe('coerceVariableValues', () => {
  const schema = buildSchema();

  it('Int! 拒绝字符串、浮点与越界整数', () => {
    const defs = defsOf('query($n: Int!) { users(limit: $n) { id } }');
    expect(coerceVariableValues(schema, defs, { n: 'abc' }).errors[0]?.message).toMatch(/\$n/);
    expect(coerceVariableValues(schema, defs, { n: 1.5 }).errors).toHaveLength(1);
    expect(coerceVariableValues(schema, defs, { n: 2147483648 }).errors).toHaveLength(1);
    expect(coerceVariableValues(schema, defs, { n: 42 }).values).toEqual({ n: 42 });
  });

  it('ID! 接受字符串与整数（整数转字符串），拒绝布尔/对象', () => {
    const defs = defsOf('query($id: ID!) { user(id: $id) { id } }');
    expect(coerceVariableValues(schema, defs, { id: 'u1' }).values).toEqual({ id: 'u1' });
    expect(coerceVariableValues(schema, defs, { id: 7 }).values).toEqual({ id: '7' });
    expect(coerceVariableValues(schema, defs, { id: true }).errors).toHaveLength(1);
  });

  it('Boolean 不接受隐式转换', () => {
    const defs = defsOf('query($b: Boolean) { users { id } }');
    // Boolean 变量没被使用也会按声明转换
    expect(coerceVariableValues(schema, defs, { b: 'true' }).errors).toHaveLength(1);
    expect(coerceVariableValues(schema, defs, { b: true }).values).toEqual({ b: true });
  });

  it('列表标量：单值包装为单元素数组；元素类型错误带 [index] 路径', () => {
    const defs = defsOf('query($xs: [Int!]) { users { id } }');
    expect(coerceVariableValues(schema, defs, { xs: 5 }).values).toEqual({ xs: [5] });
    const result = coerceVariableValues(schema, defs, { xs: [1, 'bad', 3] });
    expect(result.errors[0]?.message).toMatch(/\[1\]/);
  });

  it('非空列表收到 null => COERCION；可空列表收到 null => null', () => {
    const nn = defsOf('query($xs: [Int]!) { users { id } }');
    expect(coerceVariableValues(schema, nn, { xs: null }).errors).toHaveLength(1);
    const nullable = defsOf('query($xs: [Int]) { users { id } }');
    expect(coerceVariableValues(schema, nullable, { xs: null }).values).toEqual({ xs: null });
  });

  it('必填缺失是错误，可空缺省为 undefined（不写入），默认值生效', () => {
    const defs = defsOf('query($id: ID!, $n: Int = 3) { user(id: $id) { id } }');
    const result = coerceVariableValues(schema, defs, {});
    expect(result.errors).toHaveLength(1);
    expect(result.errors[0]?.message).toMatch(/not provided/);

    const ok = coerceVariableValues(schema, defs, { id: 'u1' });
    expect(ok.errors).toHaveLength(0);
    expect(ok.values).toEqual({ id: 'u1', n: 3 });
  });

  it('错误类别固定为 COERCION 且带位置', () => {
    const defs: OperationDefinitionNode['variableDefinitions'] = defsOf(
      'query($n: Int!) { users(limit: $n) { id } }',
    );
    const { errors } = coerceVariableValues(schema, defs, { n: null });
    expect(errors[0]?.category).toBe('COERCION');
    expect(errors[0]?.locations?.[0]?.line).toBeGreaterThanOrEqual(1);
  });
});
