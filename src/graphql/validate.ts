/**
 * 文档校验（执行前，全部 VALIDATION 类别）：
 *  - 操作/片段命名、匿名操作唯一性、subscription 拒绝
 *  - 片段目标存在、类型条件合法、片段循环（染色 DFS）
 *  - 字段/参数存在、leaf 与对象类型选择集约束
 *  - 变量：唯一定义、必须是输入类型、使用必须已定义、变量类型在参数位置可用
 *  - 字段合并：同一响应键(alias ?? name)的字段必须同名、同参数、子选择兼容
 */

import type {
  DocumentNode,
  FieldNode,
  FragmentDefinitionNode,
  OperationDefinitionNode,
  SelectionSetNode,
  ValueNode,
  VariableDefinitionNode,
} from './ast.js';
import { collectFields, detectFragmentCycles, type FragmentMap } from './collect.js';
import { GraphQLError } from './error.js';
import {
  type GraphQLObjectType,
  type GraphQLSchema,
  typeRefFromAst,
  type TypeRef,
  typeToString,
} from './schema.js';

interface VariableUsage {
  name: string;
  node: ValueNode;
  /** 使用点外层是否被非空参数要求，用于位置兼容判定 */
  argumentType: TypeRef;
  fieldName: string;
  line: number;
  column: number;
}

export function validateDocument(
  schema: GraphQLSchema,
  document: DocumentNode,
): GraphQLError[] {
  const errors: GraphQLError[] = [];
  const operations: OperationDefinitionNode[] = [];
  const fragments: FragmentMap = {};

  for (const def of document.definitions) {
    if (def.kind === 'OperationDefinition') {
      operations.push(def);
    } else {
      if (fragments[def.name.value]) {
        errors.push(
          new GraphQLError(`Duplicate fragment definition "${def.name.value}".`, {
            category: 'VALIDATION',
            locations: [{ line: def.loc.line, column: def.loc.column }],
          }),
        );
      }
      fragments[def.name.value] = def;
    }
  }

  validateOperationDefinitions(operations, errors);
  validateFragmentDefinitions(schema, fragments, errors);
  errors.push(...detectFragmentCycles(fragments));

  // 片段循环会让后续展开分析不安全；循环错误已足够，直接返回。
  if (errors.some((e) => e.message.includes('within itself'))) {
    return dedupeErrors(errors);
  }

  for (const fragment of Object.values(fragments)) {
    validateSelectionSetStructure(schema, fragments, fragment.selectionSet, fragment.typeCondition.name.value, errors);
  }
  for (const operation of operations) {
    const rootType =
      operation.operation === 'mutation' ? schema.mutationType : schema.queryType;
    if (!rootType) {
      errors.push(
        new GraphQLError(`Schema does not support ${operation.operation} operations.`, {
          category: 'VALIDATION',
          locations: [{ line: operation.loc.line, column: operation.loc.column }],
        }),
      );
      continue;
    }
    const usages: VariableUsage[] = [];
    validateSelectionSetStructure(schema, fragments, operation.selectionSet, rootType.name, errors, usages);
    validateVariables(schema, operation, usages, errors);
    validateFieldMerging(schema, fragments, operation.selectionSet, rootType.name, errors, new Set());
  }

  return dedupeErrors(errors);
}

/* ------------------------------- 操作与片段定义 ------------------------------- */

function validateOperationDefinitions(
  operations: OperationDefinitionNode[],
  errors: GraphQLError[],
): void {
  const named = new Set<string>();
  let anonymousCount = 0;
  for (const op of operations) {
    if (op.operation === 'subscription') {
      errors.push(
        new GraphQLError('Subscriptions are not supported by this server.', {
          category: 'VALIDATION',
          locations: [{ line: op.loc.line, column: op.loc.column }],
        }),
      );
    }
    if (!op.name) {
      anonymousCount += 1;
    } else {
      if (named.has(op.name.value)) {
        errors.push(
          new GraphQLError(`Duplicate operation name "${op.name.value}".`, {
            category: 'VALIDATION',
            locations: [{ line: op.name.loc.line, column: op.name.loc.column }],
          }),
        );
      }
      named.add(op.name.value);
    }
  }
  if (anonymousCount > 0 && operations.length > 1) {
    errors.push(
      new GraphQLError(
        'This anonymous operation must be the only defined operation in the document.',
        { category: 'VALIDATION' },
      ),
    );
  }
}

function validateFragmentDefinitions(
  schema: GraphQLSchema,
  fragments: FragmentMap,
  errors: GraphQLError[],
): void {
  for (const fragment of Object.values(fragments)) {
    const typeName = fragment.typeCondition.name.value;
    const type = schema.getType(typeName);
    if (!type) {
      errors.push(
        new GraphQLError(`Unknown type "${typeName}" in fragment "${fragment.name.value}".`, {
          category: 'VALIDATION',
          locations: [{ line: fragment.typeCondition.loc.line, column: fragment.typeCondition.loc.column }],
        }),
      );
      continue;
    }
    if (type.kind !== 'OBJECT') {
      errors.push(
        new GraphQLError(
          `Fragment "${fragment.name.value}" condition must be an object type but got ${type.kind} "${typeName}".`,
          {
            category: 'VALIDATION',
            locations: [{ line: fragment.typeCondition.loc.line, column: fragment.typeCondition.loc.column }],
          },
        ),
      );
    }
  }
}

/* ------------------------------- 选择集结构遍历 ------------------------------- */

function validateSelectionSetStructure(
  schema: GraphQLSchema,
  fragments: FragmentMap,
  selectionSet: SelectionSetNode,
  parentTypeName: string,
  errors: GraphQLError[],
  variableUsages?: VariableUsage[],
  visited: Set<string> = new Set(),
): void {
  const parentType = schema.getType(parentTypeName);
  if (!parentType || parentType.kind !== 'OBJECT') return;

  for (const selection of selectionSet.selections) {
    if (selection.kind === 'FragmentSpread') {
      const fragment = fragments[selection.name.value];
      if (!fragment) {
        errors.push(
          new GraphQLError(`Unknown fragment "${selection.name.value}".`, {
            category: 'VALIDATION',
            locations: [{ line: selection.loc.line, column: selection.loc.column }],
          }),
        );
        continue;
      }
      if (!visited.has(selection.name.value)) {
        visited.add(selection.name.value);
        validateSelectionSetStructure(
          schema, fragments, fragment.selectionSet,
          fragment.typeCondition.name.value, errors, variableUsages, visited,
        );
      }
      continue;
    }
    if (selection.kind === 'InlineFragment') {
      const targetName = selection.typeCondition
        ? selection.typeCondition.name.value
        : parentTypeName;
      const targetType = schema.getType(targetName);
      if (selection.typeCondition && (!targetType || targetType.kind !== 'OBJECT')) {
        errors.push(
          new GraphQLError(`Unknown type "${targetName}" in inline fragment.`, {
            category: 'VALIDATION',
            locations: [{ line: selection.typeCondition.loc.line, column: selection.typeCondition.loc.column }],
          }),
        );
        continue;
      }
      validateSelectionSetStructure(
        schema, fragments, selection.selectionSet, targetName, errors, variableUsages, visited,
      );
      continue;
    }
    validateField(schema, fragments, selection, parentType, errors, variableUsages, visited);
  }
}

function validateField(
  schema: GraphQLSchema,
  fragments: FragmentMap,
  field: FieldNode,
  parentType: GraphQLObjectType,
  errors: GraphQLError[],
  variableUsages: VariableUsage[] | undefined,
  visited: Set<string>,
): void {
  const fieldName = field.name.value;
  if (fieldName === '__typename' || fieldName === '__schema' || fieldName === '__type') {
    // 内省字段：__typename 执行期支持；__schema/__type 不在受限范围内。
    if (fieldName !== '__typename') {
      errors.push(
        new GraphQLError(`Introspection field "${fieldName}" is not supported.`, {
          category: 'VALIDATION',
          locations: [{ line: field.loc.line, column: field.loc.column }],
        }),
      );
    }
    return;
  }

  const fieldDef = parentType.fields.get(fieldName);
  if (!fieldDef) {
    errors.push(
      new GraphQLError(
        `Cannot query field "${fieldName}" on type "${parentType.name}".`,
        {
          category: 'VALIDATION',
          locations: [{ line: field.name.loc.line, column: field.name.loc.column }],
        },
      ),
    );
    return;
  }

  // 参数存在性 + 收集变量使用
  const argDefs = fieldDef.args;
  for (const arg of field.arguments) {
    const argDef = argDefs.get(arg.name.value);
    if (!argDef) {
      errors.push(
        new GraphQLError(
          `Unknown argument "${arg.name.value}" on field "${parentType.name}.${fieldName}".`,
          {
            category: 'VALIDATION',
            locations: [{ line: arg.name.loc.line, column: arg.name.loc.column }],
          },
        ),
      );
      continue;
    }
    collectVariableUsages(arg.value, argDef.type, fieldName, variableUsages);
  }

  const namedReturn = unwrapNamedType(schema, fieldDef.type);
  const isCompositeReturn = namedReturn?.kind === 'OBJECT';
  if (field.selectionSet && !isCompositeReturn) {
    errors.push(
      new GraphQLError(
        `Field "${fieldName}" of type "${typeToString(fieldDef.type)}" must not have a selection set.`,
        {
          category: 'VALIDATION',
          locations: [{ line: field.loc.line, column: field.loc.column }],
        },
      ),
    );
  }
  if (!field.selectionSet && isCompositeReturn) {
    errors.push(
      new GraphQLError(
        `Field "${fieldName}" of type "${typeToString(fieldDef.type)}" must have a selection of subfields.`,
        {
          category: 'VALIDATION',
          locations: [{ line: field.loc.line, column: field.loc.column }],
        },
      ),
    );
  }
  if (field.selectionSet && namedReturn?.kind === 'OBJECT') {
    validateSelectionSetStructure(
      schema, fragments, field.selectionSet, namedReturn.name, errors, variableUsages, visited,
    );
  }
}

function collectVariableUsages(
  value: ValueNode,
  argumentType: TypeRef,
  fieldName: string,
  usages: VariableUsage[] | undefined,
): void {
  if (!usages) return;
  if (value.kind === 'Variable') {
    usages.push({
      name: value.name.value,
      node: value,
      argumentType,
      fieldName,
      line: value.loc.line,
      column: value.loc.column,
    });
    return;
  }
  if (value.kind === 'ListValue' && argumentType.kind !== 'NAMED') {
    const inner = argumentType.kind === 'NON_NULL' ? argumentType.ofType : argumentType;
    if (inner.kind === 'LIST') {
      for (const item of value.values) {
        collectVariableUsages(item, inner.ofType, fieldName, usages);
      }
    }
  }
  if (value.kind === 'ObjectValue') {
    // 当前 schema 没有输入对象类型，保留遍历以免遗漏嵌套变量。
    for (const f of value.fields) {
      collectVariableUsages(f.value, argumentType, fieldName, usages);
    }
  }
}

/* ---------------------------------- 变量校验 ---------------------------------- */

function validateVariables(
  schema: GraphQLSchema,
  operation: OperationDefinitionNode,
  usages: VariableUsage[],
  errors: GraphQLError[],
): void {
  const definitions = new Map<string, VariableDefinitionNode>();
  for (const def of operation.variableDefinitions) {
    const name = def.variable.name.value;
    if (definitions.has(name)) {
      errors.push(
        new GraphQLError(`There can be only one variable named "$${name}".`, {
          category: 'VALIDATION',
          locations: [{ line: def.variable.loc.line, column: def.variable.loc.column }],
        }),
      );
    }
    definitions.set(name, def);

    const defType = typeRefFromAst(def.type);
    if (!isInputType(schema, defType)) {
      errors.push(
        new GraphQLError(
          `Variable "$${name}" must be of an input type, but got "${typeToString(defType)}".`,
          {
            category: 'VALIDATION',
            locations: [{ line: def.variable.loc.line, column: def.variable.loc.column }],
          },
        ),
      );
    }
  }

  const used = new Set<string>();
  for (const usage of usages) {
    used.add(usage.name);
    const def = definitions.get(usage.name);
    if (!def) {
      errors.push(
        new GraphQLError(
          `Variable "$${usage.name}" is not defined by operation "${operation.name?.value ?? 'anonymous'}".`,
          {
            category: 'VALIDATION',
            locations: [{ line: usage.line, column: usage.column }],
          },
        ),
      );
      continue;
    }
    const defType = typeRefFromAst(def.type);
    if (!isTypeAllowedInPosition(defType, usage.argumentType)) {
      errors.push(
        new GraphQLError(
          `Variable "$${usage.name}" of type "${typeToString(defType)}" used in position expecting type "${typeToString(usage.argumentType)}".`,
          {
            category: 'VALIDATION',
            locations: [{ line: usage.line, column: usage.column }],
          },
        ),
      );
    }
  }
}

function isInputType(schema: GraphQLSchema, type: TypeRef): boolean {
  switch (type.kind) {
    case 'LIST':
      return isInputType(schema, type.ofType);
    case 'NON_NULL':
      return isInputType(schema, type.ofType);
    case 'NAMED': {
      const t = schema.getType(type.name);
      return !!t && (t.kind === 'SCALAR' || t.kind === 'ENUM');
    }
  }
}

/**
 * 变量类型在参数位置是否允许（规范的 IsVariableUsageAllowed / 子类型规则的简化版）：
 * 命名类型同名；列表元素对应；非空变量可用于可空位置，反之不可。
 */
function isTypeAllowedInPosition(variableType: TypeRef, locationType: TypeRef): boolean {
  if (locationType.kind === 'NON_NULL') {
    if (variableType.kind !== 'NON_NULL') return false;
    return isTypeAllowedInPosition(variableType.ofType, locationType.ofType);
  }
  if (variableType.kind === 'NON_NULL') {
    return isTypeAllowedInPosition(variableType.ofType, locationType);
  }
  if (locationType.kind === 'LIST') {
    if (variableType.kind !== 'LIST') return false;
    return isTypeAllowedInPosition(variableType.ofType, locationType.ofType);
  }
  if (variableType.kind === 'LIST') return false;
  return variableType.kind === 'NAMED' && variableType.name === (locationType as { name: string }).name;
}

/* ---------------------------------- 字段合并冲突 -------------------------------- */

function validateFieldMerging(
  schema: GraphQLSchema,
  fragments: FragmentMap,
  selectionSet: SelectionSetNode,
  parentTypeName: string,
  errors: GraphQLError[],
  expanding: Set<string>,
): void {
  const parentType = schema.getType(parentTypeName);
  const groups = collectFields(fragments, selectionSet, parentTypeName);
  for (const [responseKey, fields] of groups) {
    if (fields.length >= 2) {
      const first = fields[0];
      for (const other of fields.slice(1)) {
        if (first.name.value !== other.name.value) {
          errors.push(
            new GraphQLError(
              `Fields "${first.name.value}"${aliasSuffix(first)} and "${other.name.value}"${aliasSuffix(other)} conflict because they use the same response key "${responseKey}" but refer to different fields.`,
              {
                category: 'VALIDATION',
                locations: fields.map((f) => ({ line: f.loc.line, column: f.loc.column })),
            },
            ),
          );
        }
        if (!sameArguments(first, other)) {
          errors.push(
            new GraphQLError(
              `Field "${responseKey}" conflict because they have differing arguments. Use distinct aliases or identical arguments.`,
              {
                category: 'VALIDATION',
                locations: fields.map((f) => ({ line: f.loc.line, column: f.loc.column })),
              },
            ),
          );
        }
      }
    }

    // 合并后的字段组只需对子选择递归一次；返回对象类型由字段定义解析。
    const merged = fields[0];
    if (!merged.selectionSet || parentType?.kind !== 'OBJECT') continue;
    if (merged.name.value === '__typename') continue;
    const fieldDef = parentType.fields.get(merged.name.value);
    if (!fieldDef) continue;
    const childTypeName = getObjectReturnTypeName(schema, fieldDef.type);
    if (!childTypeName) continue;
    const guardKey = `${childTypeName}:${merged.loc.offset}`;
    if (expanding.has(guardKey)) continue;
    expanding.add(guardKey);
    // 合并跨多个字段节点的子选择（不同别名/片段贡献不同子字段时也要检查）
    for (const f of fields) {
      if (f.selectionSet) {
        validateFieldMerging(schema, fragments, f.selectionSet, childTypeName, errors, expanding);
      }
    }
  }
}

function getObjectReturnTypeName(schema: GraphQLSchema, type: TypeRef): string | null {
  let t: TypeRef = type;
  while (t.kind !== 'NAMED') t = t.ofType;
  const namedType = schema.getType(t.name);
  return namedType?.kind === 'OBJECT' ? namedType.name : null;
}

function aliasSuffix(field: FieldNode): string {
  return field.alias ? ` (aliased as "${field.alias.value}")` : '';
}

/* ---------------------------------- AST 比较 ---------------------------------- */

function sameArguments(a: FieldNode, b: FieldNode): boolean {
  if (a.arguments.length !== b.arguments.length) return false;
  const mapB = new Map(b.arguments.map((arg) => [arg.name.value, arg.value]));
  for (const arg of a.arguments) {
    const other = mapB.get(arg.name.value);
    if (!other || !sameValueNode(arg.value, other)) return false;
  }
  return true;
}

function sameValueNode(a: ValueNode, b: ValueNode): boolean {
  if (a.kind !== b.kind) return false;
  switch (a.kind) {
    case 'Variable':
      return (b as typeof a).name.value === a.name.value;
    case 'IntValue':
    case 'FloatValue':
    case 'StringValue':
    case 'EnumValue':
    case 'BooleanValue':
      return (b as { value: unknown }).value === a.value;
    case 'NullValue':
      return true;
    case 'ListValue': {
      const bl = b as typeof a;
      return a.values.length === bl.values.length &&
        a.values.every((v, i) => sameValueNode(v, bl.values[i]));
    }
    case 'ObjectValue': {
      const bo = b as typeof a;
      if (a.fields.length !== bo.fields.length) return false;
      const mapB = new Map(bo.fields.map((f) => [f.name.value, f.value]));
      return a.fields.every((f) => {
        const other = mapB.get(f.name.value);
        return other !== undefined && sameValueNode(f.value, other);
      });
    }
  }
}

/* ---------------------------------- 类型工具 ---------------------------------- */

function unwrapNamedType(
  schema: GraphQLSchema,
  type: TypeRef,
): ReturnType<GraphQLSchema['getType']> {
  let t: TypeRef = type;
  while (t.kind !== 'NAMED') t = t.ofType;
  return schema.getType(t.name);
}

function dedupeErrors(errors: GraphQLError[]): GraphQLError[] {
  const seen = new Set<string>();
  return errors.filter((e) => {
    const key = `${e.message}:${JSON.stringify(e.locations ?? [])}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
