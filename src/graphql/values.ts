/**
 * 输入值处理：
 *  - valueFromAst：把 AST 字面量在变量环境下求值
 *  - coerceVariableValues：执行前按声明类型强制转换变量（失败即请求级 COERCION 错误）
 *  - coerceArgumentValues：字段实参（字面量/变量/默认值）逐字段强制转换
 *
 * 路径信息会进入错误消息，便于定位“哪个变量/哪个列表下标”失败。
 */

import type {
  ObjectValueNode,
  ValueNode,
  VariableDefinitionNode,
} from './ast.js';
import { GraphQLError } from './error.js';
import {
  type GraphQLSchema,
  type InputArgDef,
  isNonNull,
  typeRefFromAst,
  type TypeRef,
} from './schema.js';

export function valueFromAst(
  node: ValueNode,
  variables: Record<string, unknown>,
): unknown {
  switch (node.kind) {
    case 'Variable':
      return variables[node.name.value];
    case 'IntValue':
      return Number(node.value);
    case 'FloatValue':
      return Number(node.value);
    case 'StringValue':
    case 'EnumValue':
      return node.value;
    case 'BooleanValue':
      return node.value;
    case 'NullValue':
      return null;
    case 'ListValue':
      return node.values.map((v) => valueFromAst(v, variables));
    case 'ObjectValue':
      return objectFromAst(node, variables);
  }
}

function objectFromAst(
  node: ObjectValueNode,
  variables: Record<string, unknown>,
): Record<string, unknown> {
  const obj: Record<string, unknown> = {};
  for (const field of node.fields) {
    obj[field.name.value] = valueFromAst(field.value, variables);
  }
  return obj;
}

/**
 * 执行前变量强制转换。返回 [coerced, errors]：
 *  - 缺失的非空变量（无默认值）是请求级错误，调用方必须中止执行。
 *  - 类型不匹配（标量拒绝、列表结构错误等）同样是请求级错误。
 */
export function coerceVariableValues(
  schema: GraphQLSchema,
  definitions: readonly VariableDefinitionNode[],
  inputs: Record<string, unknown> | null | undefined,
): { values: Record<string, unknown>; errors: GraphQLError[] } {
  const values: Record<string, unknown> = {};
  const errors: GraphQLError[] = [];
  const provided = inputs ?? {};

  for (const def of definitions) {
    const name = def.variable.name.value;
    const defType = typeRefFromAst(def.type);
    const hasInput = Object.prototype.hasOwnProperty.call(provided, name);
    const input = provided[name];

    if (!hasInput || input === undefined) {
      if (def.defaultValue) {
        values[name] = valueFromAst(def.defaultValue, {});
      } else if (isNonNull(defType)) {
        errors.push(
          new GraphQLError(
            `Variable "$${name}" of required type "${typeLabel(defType)}" was not provided.`,
            {
              category: 'COERCION',
              locations: [{ line: def.variable.loc.line, column: def.variable.loc.column }],
            },
          ),
        );
      }
      continue;
    }

    if (input === null) {
      if (isNonNull(defType)) {
        errors.push(
          new GraphQLError(
            `Variable "$${name}" of non-null type "${typeLabel(defType)}" must not be null.`,
            {
              category: 'COERCION',
              locations: [{ line: def.variable.loc.line, column: def.variable.loc.column }],
            },
          ),
        );
        continue;
      }
      values[name] = null;
      continue;
    }

    try {
      values[name] = coerceInputValue(schema, defType, input, `$${name}`);
    } catch (error) {
      errors.push(
        new GraphQLError(
          `Variable "$${name}" got invalid value: ${(error as Error).message}`,
          {
            category: 'COERCION',
            locations: [{ line: def.variable.loc.line, column: def.variable.loc.column }],
          },
        ),
      );
    }
  }

  return { values, errors };
}

/**
 * 字段实参强制转换。实参字面量在执行期转换，转换错误按字段错误处理
 * （沿该字段的非空边界冒泡），变量值已在执行前完成转换。
 */
export function coerceArgumentValues(
  schema: GraphQLSchema,
  argDefs: Map<string, InputArgDef>,
  argumentNodes: readonly {
    name: { value: string };
    value: ValueNode;
  }[],
  variables: Record<string, unknown>,
): { args: Record<string, unknown>; error: GraphQLError | null } {
  const args: Record<string, unknown> = {};
  for (const [name, def] of argDefs) {
    if (def.defaultValue !== undefined) args[name] = def.defaultValue;
  }
  for (const argNode of argumentNodes) {
    const def = argDefs.get(argNode.name.value);
    if (!def) continue; // 校验阶段已拒绝未知参数
    try {
      if (argNode.value.kind === 'Variable') {
        const varName = argNode.value.name.value;
        if (Object.prototype.hasOwnProperty.call(variables, varName)) {
          args[argNode.name.value] = variables[varName];
        }
      } else {
        const raw = valueFromAst(argNode.value, variables);
        args[argNode.name.value] = coerceInputValue(
          schema,
          def.type,
          raw,
          `argument "${argNode.name.value}"`,
        );
      }
    } catch (error) {
      return {
        args,
        error: new GraphQLError(
          `Argument "${argNode.name.value}" got invalid value: ${(error as Error).message}`,
          { category: 'COERCION' },
        ),
      };
    }
  }
  return { args, error: null };
}

function coerceInputValue(
  schema: GraphQLSchema,
  type: TypeRef,
  value: unknown,
  path: string,
): unknown {
  if (type.kind === 'NON_NULL') {
    if (value === null || value === undefined) {
      throw new Error(`expected non-null value at ${path}`);
    }
    return coerceInputValue(schema, type.ofType, value, path);
  }

  if (value === null || value === undefined) return null;

  if (type.kind === 'LIST') {
    // 规范行为：单值在列表输入位置包装为单元素列表
    const items = Array.isArray(value) ? value : [value];
    return items.map((item, index) =>
      coerceInputValue(schema, type.ofType, item, `${path}[${index}]`),
    );
  }

  const namedType = schema.getType(type.name);
  if (!namedType) {
    throw new Error(`unknown type "${type.name}" at ${path}`);
  }
  if (namedType.kind === 'SCALAR') {
    try {
      return namedType.parseValue(value);
    } catch (error) {
      throw new Error(`${(error as Error).message} at ${path}`);
    }
  }
  if (namedType.kind === 'ENUM') {
    if (typeof value === 'string' && namedType.values.has(value)) return value;
    throw new Error(
      `value "${String(value)}" is not a valid enum value of "${namedType.name}" at ${path}`,
    );
  }
  throw new Error(`type "${type.name}" is not an input type at ${path}`);
}

function typeLabel(type: TypeRef): string {
  switch (type.kind) {
    case 'NAMED':
      return type.name;
    case 'LIST':
      return `[${typeLabel(type.ofType)}]`;
    case 'NON_NULL':
      return `${typeLabel(type.ofType)}!`;
  }
}
