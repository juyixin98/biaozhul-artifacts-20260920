/**
 * 执行内核。
 *
 * 关键语义（对照 GraphQL 规范 6.6 Executing Operations / 6.6.3 Normal & Serial Execution）：
 *  - query：同一选择集内字段并发解析（Promise.all）。
 *  - mutation：顶层字段严格按文档顺序逐个 await；其嵌套对象内部恢复并发。
 *  - 非空冒泡：解析器错误/null 命中 Non-Null 类型时抛 NonNullViolation，
 *    沿类型树上抛，直到可空边界落入 errors（原始错误只记录一次），
 *    或到达 data 根令 data=null。
 *  - 列表：元素非空([T!])与列表本身非空([T]!)区别处理；元素间并发。
 *  - 每个错误携带 path（响应键 + 列表下标）与 locations。
 */

import type {
  FieldNode,
  FragmentDefinitionNode,
  OperationDefinitionNode,
} from './ast.js';
import { collectFields, responseKey, type FragmentMap } from './collect.js';
import {
  GraphQLError,
  NonNullViolation,
} from './error.js';
import {
  type GraphQLContext,
  type GraphQLObjectType,
  type GraphQLSchema,
  type ResolveInfo,
  type TypeRef,
} from './schema.js';
import { coerceArgumentValues } from './values.js';

export interface ExecutionResult {
  data?: Record<string, unknown> | null;
  errors?: GraphQLError[];
}

export interface ExecuteOptions {
  schema: GraphQLSchema;
  operation: OperationDefinitionNode;
  fragments: FragmentMap;
  variables: Record<string, unknown>;
  context: GraphQLContext;
}

export async function executeOperation(options: ExecuteOptions): Promise<ExecutionResult> {
  const { schema, operation, fragments, variables, context } = options;
  const rootType =
    operation.operation === 'mutation' ? schema.mutationType : schema.queryType;
  if (!rootType) {
    return {
      data: null,
      errors: [
        new GraphQLError(`Schema does not support ${operation.operation} operations.`, {
          category: 'VALIDATION',
        }),
      ],
    };
  }

  const errors: GraphQLError[] = [];
  // 仅 mutation 顶层使用串行策略；嵌套对象内部并发。
  let data: Record<string, unknown>;
  try {
    data = await executeSelectionSet({
      schema,
      parentType: rootType,
      source: {},
      selectionSet: operation.selectionSet,
      path: [],
      fragments,
      variables,
      context,
      operation,
      errors,
      serial: operation.operation === 'mutation',
    });
  } catch (error) {
    if (error instanceof NonNullViolation) {
      errors.push(withPath(error.original, []));
      return errors.length > 0 ? { data: null, errors: dedupe(errors) } : { data: null };
    }
    throw error;
  }

  return errors.length > 0 ? { data, errors: dedupe(errors) } : { data };
}

interface SelectionSetArgs {
  schema: GraphQLSchema;
  parentType: GraphQLObjectType;
  source: unknown;
  selectionSet: OperationDefinitionNode['selectionSet'];
  path: ReadonlyArray<string | number>;
  fragments: FragmentMap;
  variables: Record<string, unknown>;
  context: GraphQLContext;
  operation: OperationDefinitionNode;
  errors: GraphQLError[];
  serial: boolean;
}

async function executeSelectionSet(args: SelectionSetArgs): Promise<Record<string, unknown>> {
  const { parentType, selectionSet, fragments, serial } = args;
  const groups = collectFields(fragments, selectionSet, parentType.name);
  const result: Record<string, unknown> = {};

  const entries = [...groups.entries()];
  if (serial) {
    for (const [key, fieldNodes] of entries) {
      await resolveOneField(key, fieldNodes, result, args);
    }
  } else {
    await Promise.all(
      entries.map(([key, fieldNodes]) => resolveOneField(key, fieldNodes, result, args)),
    );
  }
  return result;
}

async function resolveOneField(
  key: string,
  fieldNodes: FieldNode[],
  result: Record<string, unknown>,
  args: SelectionSetArgs,
): Promise<void> {
  const fieldPath = [...args.path, key];
  try {
    result[key] = await executeField(fieldNodes, args, fieldPath);
  } catch (error) {
    if (error instanceof NonNullViolation) throw error;
    // 可空边界：错误落盘，该字段为 null。
    args.errors.push(toFieldError(error, fieldNodes, fieldPath));
    result[key] = null;
  }
}

async function executeField(
  fieldNodes: FieldNode[],
  args: SelectionSetArgs,
  fieldPath: Array<string | number>,
): Promise<unknown> {
  const { schema, parentType, source, variables, context, fragments, operation } = args;
  const fieldNode = fieldNodes[0];
  const fieldName = fieldNode.name.value;

  if (fieldName === '__typename') {
    return parentType.name;
  }

  const fieldDef = parentType.fields.get(fieldName);
  if (!fieldDef) {
    throw new GraphQLError(
      `Cannot query field "${fieldName}" on type "${parentType.name}".`,
      { category: 'VALIDATION' },
    );
  }

  const { args: coercedArgs, error } = coerceArgumentValues(
    schema,
    fieldDef.args,
    fieldNode.arguments,
    variables,
  );
  if (error) throw error;

  const info: ResolveInfo = {
    fieldName,
    responseKey: responseKey(fieldNode),
    fieldNodes,
    parentTypeName: parentType.name,
    path: fieldPath,
    variableValues: variables,
    fragments,
    operation,
  };

  let resolved: unknown;
  try {
    resolved = fieldDef.resolve
      ? await fieldDef.resolve(source, coercedArgs, context, info)
      : defaultResolver(source, fieldName);
  } catch (error) {
    if (error instanceof GraphQLError) throw error;
    throw new GraphQLError(
      error instanceof Error ? error.message : 'Resolver failed',
      { category: 'RESOLVER' },
    );
  }

  try {
    const returnTypeName = namedTypeName(fieldDef.type);
    const returnType = schema.getType(returnTypeName);
    return await completeValue(fieldDef.type, resolved, {
      schema,
      fieldNodes,
      path: fieldPath,
      fragments,
      variables,
      context,
      operation,
      parentTypeName: returnType?.kind === 'OBJECT' ? returnType.name : null,
      errors: args.errors,
    });
  } catch (error) {
    if (error instanceof NonNullViolation) {
      if (fieldDef.type.kind === 'NON_NULL') throw error;
      // 字段本身可空（例如 [T!] 或 T）：此处即冒泡边界，
      // 深层非空失败在此落成 null，原始错误（带深层 path）只记录一次。
      args.errors.push(withPath(error.original, fieldPath));
      return null;
    }
    throw error;
  }
}

function defaultResolver(source: unknown, fieldName: string): unknown {
  if (source === null || source === undefined) return null;
  const record = source as Record<string, unknown>;
  const value = record[fieldName];
  return typeof value === 'function' ? value.call(source) : value;
}

interface CompleteArgs {
  schema: GraphQLSchema;
  fieldNodes: FieldNode[];
  path: Array<string | number>;
  fragments: FragmentMap;
  variables: Record<string, unknown>;
  context: GraphQLContext;
  operation: OperationDefinitionNode;
  parentTypeName: string | null;
  errors: GraphQLError[];
}

async function completeValue(
  type: TypeRef,
  value: unknown,
  args: CompleteArgs,
): Promise<unknown> {
  if (type.kind === 'NON_NULL') {
    // 非空边界：内部错误/null 一律包装为 NonNullViolation 向上冒泡。
    if (value === null || value === undefined) {
      throw new NonNullViolation(
        new GraphQLError(
          `Cannot return null for non-nullable field at path ${formatPath(args.path)}.`,
          { category: 'RESOLVER', path: args.path, locations: fieldLocations(args.fieldNodes) },
        ),
      );
    }
    try {
      return await completeValue(type.ofType, value, args);
    } catch (error) {
      if (error instanceof NonNullViolation) throw error;
      throw new NonNullViolation(toFieldError(error, args.fieldNodes, args.path));
    }
  }

  // 可空类型：null/undefined 直接为 null（不报错）。
  if (value === null || value === undefined) return null;

  if (type.kind === 'LIST') {
    if (!Array.isArray(value)) {
      throw new GraphQLError(
        `Expected array but got ${typeof value} at path ${formatPath(args.path)}.`,
        { category: 'RESOLVER', path: args.path, locations: fieldLocations(args.fieldNodes) },
      );
    }
    const results = await Promise.all(
      value.map(async (item, index) => {
        const itemPath = [...args.path, index];
        try {
          return await completeValue(type.ofType, item, { ...args, path: itemPath });
        } catch (error) {
          if (error instanceof NonNullViolation) throw error;
          // 元素本身可空：元素级错误落盘，该元素为 null。
          args.errors.push(toFieldError(error, args.fieldNodes, itemPath));
          return null;
        }
      }),
    );
    return results;
  }

  const namedType = args.schema.getType(type.name);
  if (!namedType) {
    throw new GraphQLError(`Unknown type "${type.name}".`, { category: 'INTERNAL' });
  }

  if (namedType.kind === 'SCALAR') {
    try {
      return namedType.serialize(value);
    } catch (error) {
      throw new GraphQLError(
        `Cannot serialize value as "${namedType.name}": ${(error as Error).message}`,
        { category: 'RESOLVER', path: args.path, locations: fieldLocations(args.fieldNodes) },
      );
    }
  }

  if (namedType.kind === 'ENUM') {
    if (typeof value === 'string' && namedType.values.has(value)) return value;
    throw new GraphQLError(
      `Invalid enum value "${String(value)}" for "${namedType.name}".`,
      { category: 'RESOLVER', path: args.path, locations: fieldLocations(args.fieldNodes) },
    );
  }

  // OBJECT：嵌套选择集恢复并发策略（mutation 的串行只作用于顶层）。
  if (!args.fieldNodes.some((n) => n.selectionSet)) {
    throw new GraphQLError(
      `Missing selection set for object type "${namedType.name}".`,
      { category: 'VALIDATION', path: args.path },
    );
  }
  const mergedSelectionSet = {
    kind: 'SelectionSet' as const,
    selections: args.fieldNodes.flatMap((n) => n.selectionSet?.selections ?? []),
    loc: args.fieldNodes[0].loc,
  };
  return executeSelectionSet({
    schema: args.schema,
    parentType: namedType,
    source: value,
    selectionSet: mergedSelectionSet,
    path: args.path,
    fragments: args.fragments,
    variables: args.variables,
    context: args.context,
    operation: args.operation,
    errors: args.errors,
    serial: false,
  });
}

function namedTypeName(type: TypeRef): string {
  let t: TypeRef = type;
  while (t.kind !== 'NAMED') t = t.ofType;
  return t.name;
}

function fieldLocations(fieldNodes: FieldNode[]): { line: number; column: number }[] {
  return fieldNodes.map((n) => ({ line: n.loc.line, column: n.loc.column }));
}

function toFieldError(
  error: unknown,
  fieldNodes: FieldNode[],
  path: ReadonlyArray<string | number>,
): GraphQLError {
  if (error instanceof GraphQLError) {
    return withPath(error, path, error.path ? undefined : fieldLocations(fieldNodes));
  }
  return new GraphQLError(error instanceof Error ? error.message : String(error), {
    category: 'RESOLVER',
    path,
    locations: fieldLocations(fieldNodes),
  });
}

function withPath(
  error: GraphQLError,
  path: ReadonlyArray<string | number>,
  locations?: { line: number; column: number }[],
): GraphQLError {
  if (error.path && error.path.length > 0) return error; // 保留深层原始路径
  return new GraphQLError(error.message, {
    category: error.category,
    path,
    locations: error.locations ?? locations,
    extensions: error.extensions,
  });
}

function formatPath(path: ReadonlyArray<string | number>): string {
  return path
    .map((segment) => (typeof segment === 'number' ? `[${segment}]` : `.${segment}`))
    .join('')
    .replace(/^\./, '');
}

function dedupe(errors: GraphQLError[]): GraphQLError[] {
  const seen = new Set<string>();
  return errors.filter((error) => {
    const key = `${error.message}|${JSON.stringify(error.path ?? [])}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
