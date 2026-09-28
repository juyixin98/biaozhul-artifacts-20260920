/**
 * 选择集的类型与结构校验（在片段展开之后运行）。
 *
 * - 根类型必须存在；
 * - 字段必须在当前类型上声明；关系必须存在；
 * - 同一层级别名唯一（重复别名是输入错误，防止结果键互相覆盖）；
 * - 列表 children 必须非空（空选择无意义）；
 * - declaredUpperBound 必须是正整数（未知规模靠声明上界，禁止 0/负数/小数）。
 */
import type { FieldNode } from '../contracts/ast.js';
import type { Schema } from '../contracts/schema.js';
import { fail } from '../contracts/errors.js';

export function validateSelection(
  fields: FieldNode[],
  schema: Schema,
  typeName: string,
  path: string[],
): void {
  const typeDef = schema.types[typeName];
  if (!typeDef) {
    fail('INPUT_ERROR', 'UNKNOWN_TYPE', `unknown type: ${typeName}`, path);
  }
  const aliases = new Set<string>();
  for (const node of fields) {
    if (node.kind === 'spread') {
      fail('INPUT_ERROR', 'MALFORMED_QUERY', 'spread survived expansion', [...path]);
    }
    if (aliases.has(node.alias)) {
      fail(
        'INPUT_ERROR',
        'DUPLICATE_ALIAS',
        `duplicate alias "${node.alias}" under type "${typeName}"`,
        [...path, node.alias],
      );
    }
    aliases.add(node.alias);

    if (node.kind === 'field') {
      if (!typeDef.fields[node.field]) {
        fail(
          'INPUT_ERROR',
          'UNKNOWN_FIELD',
          `type "${typeName}" has no field "${node.field}"`,
          [...path, node.alias],
        );
      }
      continue;
    }

    // list
    if (!Number.isInteger(node.declaredUpperBound) || node.declaredUpperBound <= 0) {
      fail(
        'INPUT_ERROR',
        'INVALID_BOUND',
        `list "${node.alias}" requires a positive integer declaredUpperBound`,
        [...path, node.alias],
        { declaredUpperBound: node.declaredUpperBound },
      );
    }
    const rel = typeDef.relations[node.relation];
    if (!rel) {
      fail(
        'INPUT_ERROR',
        'UNKNOWN_RELATION',
        `type "${typeName}" has no relation "${node.relation}"`,
        [...path, node.alias],
      );
    }
    if (node.children.length === 0) {
      fail('INPUT_ERROR', 'EMPTY_SELECTION', `list "${node.alias}" has an empty selection`, [
        ...path,
        node.alias,
      ]);
    }
    validateSelection(node.children, schema, rel.to, [...path, node.alias]);
  }
}
