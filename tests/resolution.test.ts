/**
 * 契约解析与静态估计的单元测试。
 * 预期数字全部手工计算（独立参考答案），不调用执行内核生成。
 */
import { describe, expect, it } from 'vitest';
import { parseQuery } from '../src/resolution/parse.js';
import { validateVars, validateVarArgs } from '../src/resolution/variables.js';
import { expandFragments, expandTopLevelIncludes } from '../src/resolution/fragments.js';
import { validateSelection } from '../src/resolution/validate.js';
import { estimateStatic } from '../src/kernel/estimator.js';
import { QueryError } from '../src/contracts/errors.js';
import { FIXTURE_SCHEMA } from '../src/fixtures/schema.js';
import type { Query } from '../src/contracts/ast.js';

/** 跑完整静态管线（测试专用，与内核中的 resolveStatic 同序但独立书写）。 */
function staticCost(raw: unknown): number {
  const q: Query = parseQuery(raw);
  const vars = validateVars(q);
  const top = q.fragments ? expandTopLevelIncludes(q.fragments, FIXTURE_SCHEMA) : [];
  const expanded = [...top, ...expandFragments(q.fields, FIXTURE_SCHEMA)];
  validateSelection(expanded, FIXTURE_SCHEMA, q.root, []);
  validateVarArgs(expanded, FIXTURE_SCHEMA, q.root, vars, []);
  return estimateStatic(q.root, expanded, FIXTURE_SCHEMA).total;
}

function expectError(fn: () => unknown, category: string, code: string): QueryError {
  try {
    fn();
  } catch (err) {
    expect(err).toBeInstanceOf(QueryError);
    const qe = err as QueryError;
    expect(qe.diagnostic.category).toBe(category);
    expect(qe.diagnostic.code).toBe(code);
    return qe;
  }
  throw new Error(`expected ${category}/${code} but nothing was thrown`);
}

describe('变量类型校验先于执行', () => {
  it('拒绝声明 int 但给字符串的变量', () => {
    const qe = expectError(
      () =>
        validateVars(
          parseQuery({
            root: 'Org',
            rootId: 1,
            fields: [{ kind: 'field', alias: 'name', field: 'name' }],
            vars: [{ name: 'orgId', type: 'int', value: '1' }],
          }),
        ),
      'INPUT_ERROR',
      'VAR_TYPE_MISMATCH',
    );
    expect(qe.diagnostic.details).toMatchObject({ var: 'orgId', declaredType: 'int' });
  });

  it('拒绝重复变量声明', () => {
    expectError(
      () =>
        validateVars(
          parseQuery({
            root: 'Org',
            rootId: 1,
            fields: [],
            vars: [
              { name: 'x', type: 'int', value: 1 },
              { name: 'x', type: 'int', value: 2 },
            ],
          }),
        ),
      'INPUT_ERROR',
      'DUPLICATE_VAR',
    );
  });

  it('过滤变量与字段类型不兼容时报 VAR_ARG_TYPE_MISMATCH', () => {
    expectError(
      () =>
        staticCost({
          root: 'Org',
          rootId: 1,
          fields: [
            {
              kind: 'list',
              alias: 'tasks',
              relation: 'orgTasks',
              declaredUpperBound: 6,
              args: [{ var: 'orgId', op: 'eq' }],
              children: [{ kind: 'field', alias: 'title', field: 'title' }],
            },
          ],
          vars: [{ name: 'orgId', type: 'string', value: '1' }],
        }),
      'INPUT_ERROR',
      'VAR_ARG_TYPE_MISMATCH',
    );
  });

  it('引用未声明变量时报 UNKNOWN_VAR', () => {
    expectError(
      () =>
        staticCost({
          root: 'Org',
          rootId: 1,
          fields: [
            {
              kind: 'list',
              alias: 'tasks',
              relation: 'orgTasks',
              declaredUpperBound: 6,
              args: [{ var: 'missing', op: 'eq' }],
              children: [{ kind: 'field', alias: 'title', field: 'title' }],
            },
          ],
        }),
      'INPUT_ERROR',
      'UNKNOWN_VAR',
    );
  });
});

describe('片段展开', () => {
  it('循环片段 A->B->A 被检测为 FRAGMENT_CYCLE 而非栈溢出', () => {
    const qe = expectError(
      () =>
        staticCost({
          root: 'Org',
          rootId: 1,
          fields: [{ kind: 'spread', fragment: 'loopA' }],
        }),
      'INPUT_ERROR',
      'FRAGMENT_CYCLE',
    );
    expect(qe.diagnostic.details).toMatchObject({ cycle: ['loopA', 'loopB', 'loopA'] });
  });

  it('顶层 include inline 片段被拒绝', () => {
    expectError(
      () =>
        staticCost({ root: 'Org', rootId: 1, fields: [], fragments: ['loopA'] }),
      'INPUT_ERROR',
      'INLINE_FRAGMENT_FORBIDDEN',
    );
  });

  it('引用不存在片段报 UNKNOWN_FRAGMENT', () => {
    expectError(
      () =>
        staticCost({
          root: 'Org',
          rootId: 1,
          fields: [{ kind: 'spread', fragment: 'nope' }],
        }),
      'INPUT_ERROR',
      'UNKNOWN_FRAGMENT',
    );
  });

  it('同一具名片段在两个分支展开时两边都计费（重复不绕过预算）', () => {
    // taskCore = title(1) + body(3) = 4/任务
    // orgTasks bound 2：2*4 = 8
    // members bound 2 -> tasks bound 2：2*2*4 = 16
    // 合计 24；若展开被记忆化/去重，两个分支不会各拿一份 4。
    const cost = staticCost({
      root: 'Org',
      rootId: 1,
      fields: [
        {
          kind: 'list',
          alias: 'orgTasks',
          relation: 'orgTasks',
          declaredUpperBound: 2,
          children: [{ kind: 'spread', fragment: 'taskCore' }],
        },
        {
          kind: 'list',
          alias: 'members',
          relation: 'members',
          declaredUpperBound: 2,
          children: [
            {
              kind: 'list',
              alias: 'tasks',
              relation: 'tasks',
              declaredUpperBound: 2,
              children: [{ kind: 'spread', fragment: 'taskCore' }],
            },
          ],
        },
      ],
    });
    expect(cost).toBe(24);
  });

  it('同层重复 include 同一片段会产生重复别名，被 DUPLICATE_ALIAS 拦截', () => {
    // 重复计费与重复别名是两条独立规则：重复本身不去重，但同层结果键
    // 冲突仍然是输入错误。
    expectError(
      () =>
        staticCost({
          root: 'Org',
          rootId: 1,
          fields: [],
          fragments: ['orgSummary', 'orgSummary'],
        }),
      'INPUT_ERROR',
      'DUPLICATE_ALIAS',
    );
  });
});

describe('选择集与结构校验', () => {
  it('同层重复别名报 DUPLICATE_ALIAS', () => {
    expectError(
      () =>
        staticCost({
          root: 'Org',
          rootId: 1,
          fields: [
            { kind: 'field', alias: 'n', field: 'name' },
            { kind: 'field', alias: 'n', field: 'plan' },
          ],
        }),
      'INPUT_ERROR',
      'DUPLICATE_ALIAS',
    );
  });

  it('未知字段/未知关系/未知根类型分别报错', () => {
    expectError(
      () => staticCost({ root: 'Org', rootId: 1, fields: [{ kind: 'field', alias: 'x', field: 'nope' }] }),
      'INPUT_ERROR',
      'UNKNOWN_FIELD',
    );
    expectError(
      () =>
        staticCost({
          root: 'Org',
          rootId: 1,
          fields: [
            { kind: 'list', alias: 'x', relation: 'nope', declaredUpperBound: 1, children: [] },
          ],
        }),
      'INPUT_ERROR',
      'UNKNOWN_RELATION',
    );
    expectError(
      () => staticCost({ root: 'Nope', rootId: 1, fields: [] }),
      'INPUT_ERROR',
      'UNKNOWN_TYPE',
    );
  });

  it('声明上界必须是正整数；空选择被拒绝', () => {
    expectError(
      () =>
        staticCost({
          root: 'Org',
          rootId: 1,
          fields: [
            { kind: 'list', alias: 'm', relation: 'members', declaredUpperBound: 0, children: [] },
          ],
        }),
      'INPUT_ERROR',
      'INVALID_BOUND',
    );
    expectError(
      () =>
        staticCost({
          root: 'Org',
          rootId: 1,
          fields: [
            { kind: 'list', alias: 'm', relation: 'members', declaredUpperBound: 2, children: [] },
          ],
        }),
      'INPUT_ERROR',
      'EMPTY_SELECTION',
    );
  });

  it('畸形 JSON 输入报 MALFORMED_QUERY', () => {
    expectError(() => parseQuery({ root: 'Org' }), 'INPUT_ERROR', 'MALFORMED_QUERY');
    expectError(() => parseQuery('not-an-object'), 'INPUT_ERROR', 'MALFORMED_QUERY');
    expectError(
      () => parseQuery({ root: 'Org', rootId: 1, fields: [{ kind: 'weird', alias: 'a' }] }),
      'INPUT_ERROR',
      'MALFORMED_QUERY',
    );
  });
});

describe('静态成本：片段展开 × 列表基数 × 字段倍率', () => {
  it('叶子倍率直接相加：name(1)+plan(2)+heavyReport(5)=8', () => {
    const cost = staticCost({
      root: 'Org',
      rootId: 1,
      fields: [
        { kind: 'field', alias: 'name', field: 'name' },
        { kind: 'field', alias: 'plan', field: 'plan' },
        { kind: 'field', alias: 'heavy', field: 'heavyReport' },
      ],
    });
    expect(cost).toBe(8);
  });

  it('未知列表规模按声明上界乘进子树', () => {
    // members bound 3，子字段 name(1)、role(1) => 3*2 = 6
    const cost = staticCost({
      root: 'Org',
      rootId: 1,
      fields: [
        {
          kind: 'list',
          alias: 'members',
          relation: 'members',
          declaredUpperBound: 3,
          children: [
            { kind: 'field', alias: 'name', field: 'name' },
            { kind: 'field', alias: 'role', field: 'role' },
          ],
        },
      ],
    });
    expect(cost).toBe(6);
  });

  it('顶层 include 片段按其字段计费：orgSummary = 1+2 = 3', () => {
    const cost = staticCost({ root: 'Org', rootId: 1, fields: [], fragments: ['orgSummary'] });
    expect(cost).toBe(3);
  });

  it('成本不看查询字符数：多余空白与未知冗余键不改变估计', () => {
    const a = JSON.stringify({
      root: 'Org',
      rootId: 1,
      fields: [{ kind: 'field', alias: 'name', field: 'name' }],
    });
    const b = `\n\n  { "root" : "Org" , "rootId":1 ,
      "description" : "this whole verbose sentence is never counted" ,
      "fields" : [ { "kind":"field" , "alias":"name", "field":"name" } ] }\n`;
    expect(staticCost(JSON.parse(b))).toBe(staticCost(JSON.parse(a)));
    expect(staticCost(JSON.parse(a))).toBe(1);
  });
});
