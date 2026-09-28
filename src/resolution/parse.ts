/**
 * 结构化输入解析。
 *
 * 查询以 JSON 数据进入（HTTP body 或夹具对象）。这里只做“形状”校验，
 * 把不可信数据收窄为 Query；类型/字段/片段等语义校验由 resolution 其余
 * 部分负责。任何形状不符都报 INPUT_ERROR / MALFORMED_QUERY。
 */
import type { FieldNode, Query, VarDecl } from '../contracts/ast.js';
import { fail } from '../contracts/errors.js';

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function parseVar(v: unknown): VarDecl {
  if (!isObj(v)) fail('INPUT_ERROR', 'MALFORMED_QUERY', 'variable must be an object');
  const { name, type, value } = v;
  if (typeof name !== 'string') fail('INPUT_ERROR', 'MALFORMED_QUERY', 'variable.name must be string');
  if (type !== 'string' && type !== 'int' && type !== 'bool') {
    fail('INPUT_ERROR', 'MALFORMED_QUERY', `variable.type invalid: ${String(type)}`, undefined, {
      var: name,
    });
  }
  if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') {
    fail('INPUT_ERROR', 'MALFORMED_QUERY', 'variable.value must be string|int|bool', undefined, {
      var: name,
    });
  }
  return { name, type, value };
}

function parseNode(n: unknown): FieldNode {
  if (!isObj(n)) fail('INPUT_ERROR', 'MALFORMED_QUERY', 'field must be an object');
  const kind = n.kind;
  if (kind === 'spread') {
    if (typeof n.fragment !== 'string') {
      fail('INPUT_ERROR', 'MALFORMED_QUERY', 'spread requires a fragment name');
    }
    return { kind, fragment: n.fragment };
  }
  if (typeof n.alias !== 'string' || n.alias.length === 0) {
    fail('INPUT_ERROR', 'MALFORMED_QUERY', 'field alias must be a non-empty string');
  }
  const alias: string = n.alias;
  if (kind === 'field') {
    if (typeof n.field !== 'string') {
      fail('INPUT_ERROR', 'MALFORMED_QUERY', `field "${alias}" requires a field name`);
    }
    return { kind, alias, field: n.field };
  }
  if (kind === 'list') {
    if (typeof n.relation !== 'string') {
      fail('INPUT_ERROR', 'MALFORMED_QUERY', `list "${alias}" requires a relation name`);
    }
    if (typeof n.declaredUpperBound !== 'number') {
      fail('INPUT_ERROR', 'MALFORMED_QUERY', `list "${alias}" requires declaredUpperBound`, [
        alias,
      ]);
    }
    if (!Array.isArray(n.children)) {
      fail('INPUT_ERROR', 'MALFORMED_QUERY', `list "${alias}" children must be an array`, [
        alias,
      ]);
    }
    const relation: string = n.relation;
    const bound: number = n.declaredUpperBound;
    const args = Array.isArray(n.args)
      ? n.args.map((a: unknown) => {
          if (!isObj(a) || typeof a.var !== 'string') {
            fail('INPUT_ERROR', 'MALFORMED_QUERY', `list "${alias}" has a malformed arg`, [
              alias,
            ]);
          }
          return { var: a.var, op: 'eq' as const };
        })
      : undefined;
    return {
      kind,
      alias,
      relation,
      declaredUpperBound: bound,
      children: n.children.map(parseNode),
      args,
    };
  }
  fail('INPUT_ERROR', 'MALFORMED_QUERY', `unknown field kind: ${String(kind)}`);
}

export function parseQuery(input: unknown): Query {
  if (!isObj(input)) {
    fail('INPUT_ERROR', 'MALFORMED_QUERY', 'query must be a JSON object');
  }
  if (typeof input.root !== 'string') {
    fail('INPUT_ERROR', 'MALFORMED_QUERY', 'query.root must be a type name string');
  }
  if (typeof input.rootId !== 'number' || !Number.isInteger(input.rootId)) {
    fail('INPUT_ERROR', 'MALFORMED_QUERY', 'query.rootId must be an integer');
  }
  if (!Array.isArray(input.fields)) {
    fail('INPUT_ERROR', 'MALFORMED_QUERY', 'query.fields must be an array');
  }
  const query: Query = {
    root: input.root,
    rootId: input.rootId,
    fields: input.fields.map(parseNode),
  };
  if (input.vars !== undefined) {
    if (!Array.isArray(input.vars)) {
      fail('INPUT_ERROR', 'MALFORMED_QUERY', 'query.vars must be an array');
    }
    query.vars = input.vars.map(parseVar);
  }
  if (input.fragments !== undefined) {
    if (
      !Array.isArray(input.fragments) ||
      input.fragments.some((f) => typeof f !== 'string')
    ) {
      fail('INPUT_ERROR', 'MALFORMED_QUERY', 'query.fragments must be string[]');
    }
    query.fragments = [...input.fragments];
  }
  return query;
}
