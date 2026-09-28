/**
 * 变量契约解析：运行前先完成类型校验。
 *
 * 规则：
 * - 变量必须声明 name/type/value，且名字唯一；
 * - value 的 JS 类型必须与声明类型一致（int 要求 Number.isInteger）；
 * - 列表 args 引用的变量必须存在，且变量类型必须与被过滤字段的
 *   标量类型兼容（bool 不能去比 string 字段等）。
 */
import type { Query, VarDecl, VarValue } from '../contracts/ast.js';
import type { Schema } from '../contracts/schema.js';
import { fail } from '../contracts/errors.js';

const TYPE_CHECKERS: Record<VarDecl['type'], (v: VarValue) => boolean> = {
  string: (v) => typeof v === 'string',
  int: (v) => typeof v === 'number' && Number.isInteger(v),
  bool: (v) => typeof v === 'boolean',
};

/** 校验变量声明并返回只读变量表。 */
export function validateVars(query: Query): Map<string, VarValue> {
  const map = new Map<string, VarValue>();
  for (const v of query.vars ?? []) {
    if (!v || typeof v.name !== 'string' || v.name.length === 0) {
      fail('INPUT_ERROR', 'MALFORMED_QUERY', 'variable declaration requires a non-empty name');
    }
    if (map.has(v.name)) {
      fail('INPUT_ERROR', 'DUPLICATE_VAR', `duplicate variable declaration: ${v.name}`, undefined, {
        var: v.name,
      });
    }
    const checker = TYPE_CHECKERS[v.type];
    if (!checker) {
      fail('INPUT_ERROR', 'MALFORMED_QUERY', `unknown variable type: ${String(v.type)}`, undefined, {
        var: v.name,
      });
    }
    if (!checker(v.value)) {
      fail(
        'INPUT_ERROR',
        'VAR_TYPE_MISMATCH',
        `variable "${v.name}" declares ${v.type} but value ${JSON.stringify(v.value)} is not`,
        undefined,
        { var: v.name, declaredType: v.type, value: v.value },
      );
    }
    map.set(v.name, v.value);
  }
  return map;
}

/**
 * 递归校验所有列表 args 的变量引用与字段类型兼容性。
 * 必须在片段展开之后运行（spread 里的 args 也要覆盖）。
 */
export function validateVarArgs(
  fields: import('../contracts/ast.js').FieldNode[],
  schema: Schema,
  typeName: string,
  vars: ReadonlyMap<string, VarValue>,
  path: string[],
): void {
  const typeDef = schema.types[typeName];
  if (!typeDef) {
    fail('INPUT_ERROR', 'UNKNOWN_TYPE', `unknown type: ${typeName}`, path);
  }
  for (const node of fields) {
    if (node.kind === 'spread') continue; // 展开后的树不会再有 spread
    if (node.kind === 'field') {
      const fieldDef = typeDef.fields[node.field];
      if (!fieldDef) {
        fail('INPUT_ERROR', 'UNKNOWN_FIELD', `type "${typeName}" has no field "${node.field}"`, [
          ...path,
          node.alias,
        ]);
      }
      continue;
    }
    // list
    const rel = typeDef.relations[node.relation];
    if (!rel) {
      fail(
        'INPUT_ERROR',
        'UNKNOWN_RELATION',
        `type "${typeName}" has no relation "${node.relation}"`,
        [...path, node.alias],
      );
    }
    for (const arg of node.args ?? []) {
      if (!vars.has(arg.var)) {
        fail(
          'INPUT_ERROR',
          'UNKNOWN_VAR',
          `list "${node.alias}" references undeclared variable "${arg.var}"`,
          [...path, node.alias],
          { var: arg.var },
        );
      }
      // 约定：过滤目标字段名与变量同名（如 orgId）。
      const targetField = rel.to && schema.types[rel.to]?.fields[arg.var];
      // args 过滤的是“子对象上的字段”；若该字段不存在，属于未知字段。
      if (!targetField) {
        fail(
          'INPUT_ERROR',
          'UNKNOWN_FIELD',
          `cannot filter relation "${node.relation}" by "${arg.var}": field not on "${rel.to}"`,
          [...path, node.alias],
          { var: arg.var },
        );
      }
      const value = vars.get(arg.var)!;
      const actualType =
        typeof value === 'string' ? 'string' : typeof value === 'boolean' ? 'bool' : 'int';
      if (actualType !== targetField.type) {
        fail(
          'INPUT_ERROR',
          'VAR_ARG_TYPE_MISMATCH',
          `argument "${arg.var}" on "${node.alias}" is ${actualType} but field is ${targetField.type}`,
          [...path, node.alias],
          { var: arg.var, actualType, expectedType: targetField.type },
        );
      }
    }
    validateVarArgs(node.children, schema, rel.to, vars, [...path, node.alias]);
  }
}
