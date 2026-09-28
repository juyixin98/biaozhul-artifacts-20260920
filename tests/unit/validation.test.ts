/**
 * 校验器单测：片段循环、字段合并（按响应键）、变量位置、未知片段等。
 */

import { describe, expect, it } from 'vitest';
import { parse } from '../../src/graphql/parser.js';
import { validateDocument } from '../../src/graphql/validate.js';
import { detectFragmentCycles } from '../../src/graphql/collect.js';
import { buildSchema } from '../../src/state/schema.js';

const schema = buildSchema();

function validate(source: string) {
  return validateDocument(schema, parse(source));
}

function fragmentMapOf(source: string) {
  const doc = parse(source);
  const map: Record<string, ReturnType<typeof Object>> = {};
  for (const def of doc.definitions) {
    if (def.kind === 'FragmentDefinition') map[def.name.value] = def;
  }
  return map as never;
}

describe('片段循环检测', () => {
  it('检测直接自引用', () => {
    const errors = detectFragmentCycles(
      fragmentMapOf('fragment A on User { ...A }'),
    );
    expect(errors).toHaveLength(1);
    expect(errors[0]?.message).toMatch(/A -> A/);
    expect(errors[0]?.category).toBe('VALIDATION');
  });

  it('检测 A->B->A 环并给出展开链', () => {
    const errors = detectFragmentCycles(
      fragmentMapOf('fragment A on User { ...B } fragment B on User { ...A }'),
    );
    expect(errors.length).toBeGreaterThanOrEqual(1);
    expect(errors[0]?.message).toMatch(/within itself/);
    expect(errors[0]?.message).toMatch(/A/);
  });

  it('检测更深的 A->B->C->A', () => {
    const errors = detectFragmentCycles(
      fragmentMapOf(
        'fragment A on User { ...B } fragment B on User { ...C } fragment C on User { ...A }',
      ),
    );
    expect(errors.length).toBeGreaterThanOrEqual(1);
  });

  it('无环片段不报错', () => {
    const errors = detectFragmentCycles(
      fragmentMapOf(
        'fragment A on User { ...B id } fragment B on User { name }',
      ),
    );
    expect(errors).toHaveLength(0);
  });
});

describe('字段合并冲突（按响应键）', () => {
  it('别名使 id 与 name 共用响应键 id => 冲突', () => {
    const errors = validate('{ user(id: "u1") { id id: name } }');
    expect(errors.some((e) => e.message.includes('same response key'))).toBe(true);
  });

  it('同响应键同字段但参数不同 => 冲突', () => {
    const errors = validate(
      '{ a: user(id: "u1") { name } a: user(id: "u2") { name } }',
    );
    expect(errors.some((e) => e.message.includes('differing arguments'))).toBe(true);
  });

  it('通过片段制造的同响应键异字段冲突也能检出', () => {
    const errors = validate(`
      { user(id: "u1") { ...F1 ...F2 } }
      fragment F1 on User { x: id }
      fragment F2 on User { x: name }
    `);
    expect(errors.some((e) => e.message.includes('same response key'))).toBe(true);
  });

  it('同字段同参数的重复选择合法', () => {
    const errors = validate('{ user(id: "u1") { name name } }');
    expect(errors).toHaveLength(0);
  });

  it('不同别名指向同字段同参数合法', () => {
    const errors = validate('{ user(id: "u1") { a: name b: name } }');
    expect(errors).toHaveLength(0);
  });
});

describe('变量与结构校验', () => {
  it('未定义变量被使用 => VALIDATION', () => {
    const errors = validate('{ user(id: $missing) { id } }');
    expect(errors[0]?.message).toMatch(/not defined/);
  });

  it('变量类型在参数位置不兼容 => VALIDATION', () => {
    const errors = validate('query($x: String) { users(limit: $x) { id } }');
    expect(errors[0]?.message).toMatch(/used in position expecting type/);
  });

  it('非空变量可用于可空参数位置（逆变允许）', () => {
    const errors = validate('query($n: Int!) { users(limit: $n) { id } }');
    expect(errors).toHaveLength(0);
  });

  it('变量定义为对象类型 => 拒绝（必须是输入类型）', () => {
    const errors = validate('query($u: User) { user(id: "u1") { id } }');
    expect(errors.some((e) => e.message.includes('input type'))).toBe(true);
  });

  it('未知片段展开 => VALIDATION', () => {
    const errors = validate('{ user(id: "u1") { ...Nope } }');
    expect(errors[0]?.message).toMatch(/Unknown fragment/);
  });

  it('未知类型条件 => VALIDATION', () => {
    const errors = validate(
      '{ user(id: "u1") { ... on Ghost { id } } }',
    );
    expect(errors.some((e) => e.message.includes('Unknown type'))).toBe(true);
  });

  it('标量字段带选择集 => VALIDATION', () => {
    const errors = validate('{ users { name { x } } }');
    expect(errors.some((e) => e.message.includes('must not have a selection'))).toBe(true);
  });

  it('对象字段缺选择集 => VALIDATION', () => {
    const errors = validate('{ user(id: "u1") { bestFriend } }');
    expect(errors.some((e) => e.message.includes('must have a selection'))).toBe(true);
  });

  it('subscription 被拒绝', () => {
    const errors = validate('subscription { users { id } }');
    expect(errors.some((e) => e.message.includes('Subscriptions'))).toBe(true);
  });

  it('多操作且匿名 => VALIDATION', () => {
    const errors = validate('{ users { id } } query Q { users { id } }');
    expect(errors.some((e) => e.message.includes('only defined operation'))).toBe(true);
  });
});
