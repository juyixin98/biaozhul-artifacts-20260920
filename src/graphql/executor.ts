/**
 * 执行内核。
 *
 * 关键语义（本工程验收重点）：
 * 1. query 顶层字段并发执行；mutation 顶层字段严格按文档顺序逐个 await；
 *    对象类型内部字段始终并发。
 * 2. 非空失败沿类型树冒泡：标量/对象字段非空失败 -> 父字段置 null；
 *    列表元素非空失败 -> 整个列表置 null（列表可空则停在列表）；
 *    列表本身非空而解析器给 null -> 直接在字段位置冒泡。
 * 3. 每个错误带 path 与 locations；已成功的兄弟字段照常返回（部分数据）。
 * 4. 内核不认识 HTTP，也不直接碰 SQLite——状态经 schema.resolvers 注入。
 */
import type {
  DocumentNode,
  FieldNode,
  FragmentDefinitionNode,
  OperationDefinitionNode,
} from './ast.js';
import { collectFields, type CollectedField } from './collect.js';
import { coerceLiteral, coerceOutputScalar, coerceVariable, typeNodeToRef } from './coercion.js';
import { GraphQLError, isBubble, NON_NULL_BUBBLE } from './errors.js';
import { LexerError } from './lexer.js';
import { parse, ParseError } from './parser.js';
import { getResolver, type GraphQLSchema, type SchemaTypeView } from './schema.js';
import { type FieldDef, type NamedType, type ObjectType, type TypeRef } from './types.js';
import { typeRefToString } from './types.js';
import { validateDocument } from './validator.js';

export interface ExecutionResult {
  data: Record<string, unknown> | null;
  errors?: GraphQLError[];
}

export interface PreparedOperation {
  document: DocumentNode;
  operation: OperationDefinitionNode;
  fragments: Map<string, FragmentDefinitionNode>;
  coercedVariables: Record<string, unknown>;
}

interface ExecutionContext<TContext> {
  schema: GraphQLSchema<TContext>;
  appContext: TContext;
  fragments: Map<string, FragmentDefinitionNode>;
  variables: Record<string, unknown>;
  errors: GraphQLError[];
}

export type PathSegment = string | number;

// ---- 准备阶段：解析 + 校验 + 变量强制 ----

export interface PrepareOptions {
  operationName?: string | null;
  variables?: unknown;
}

export function prepareExecution<TContext>(
  schema: GraphQLSchema<TContext>,
  source: string,
  options: PrepareOptions = {},
): PreparedOperation {
  let document: DocumentNode;
  try {
    document = parse(source);
  } catch (err) {
    if (err instanceof ParseError) {
      throw new GraphQLError(err.message, 'SYNTAX', { locations: err.loc });
    }
    if (err instanceof LexerError) {
      throw new GraphQLError(err.message, 'SYNTAX', { locations: err.loc });
    }
    throw err;
  }

  const fragments = new Map<string, FragmentDefinitionNode>();
  const operations: OperationDefinitionNode[] = [];
  for (const def of document.definitions) {
    if (def.kind === 'FragmentDefinition') fragments.set(def.name.value, def);
    else operations.push(def);
  }

  const operation = selectOperation(operations, options.operationName ?? null);
  const validationErrors = validateDocument(schema as SchemaTypeView, document);
  if (validationErrors.length > 0) {
    throw new ValidationFailure(validationErrors);
  }

  const coercedVariables = coerceOperationVariables(
    schema as SchemaTypeView,
    operation,
    options.variables,
  );
  return { document, operation, fragments, coercedVariables };
}

export class ValidationFailure extends Error {
  constructor(readonly validationErrors: GraphQLError[]) {
    super(`Validation failed with ${validationErrors.length} error(s)`);
    this.name = 'ValidationFailure';
  }
}

function selectOperation(
  operations: OperationDefinitionNode[],
  operationName: string | null,
): OperationDefinitionNode {
  if (operationName === null) {
    if (operations.length === 1) return operations[0]!;
    if (operations.length === 0) {
      throw new GraphQLError('Document does not contain any operation', 'VALIDATION');
    }
    throw new GraphQLError(
      'Must provide operation name when the document contains multiple operations',
      'AMBIGUOUS_OPERATION',
    );
  }
  const match = operations.find((op) => op.name?.value === operationName);
  if (!match) {
    throw new GraphQLError(`Unknown operation named "${operationName}"`, 'UNKNOWN_OPERATION');
  }
  return match;
}

function coerceOperationVariables(
  schema: SchemaTypeView,
  operation: OperationDefinitionNode,
  raw: unknown,
): Record<string, unknown> {
  const variables: Record<string, unknown> = {};
  if (raw !== null && raw !== undefined) {
    if (typeof raw !== 'object' || Array.isArray(raw)) {
      throw new GraphQLError(
        'Variable values must be provided as an object mapping variable names to values',
        'BAD_REQUEST',
      );
    }
  }
  const input = (raw ?? {}) as Record<string, unknown>;

  for (const def of operation.variableDefinitions) {
    const name = def.variable.name.value;
    const ref = typeNodeToRef(def.type);
    const provided = Object.prototype.hasOwnProperty.call(input, name)
      ? input[name]
      : undefined;

    if (provided === undefined) {
      if (def.defaultValue) {
        variables[name] = coerceLiteral(
          def.defaultValue,
          ref,
          {},
          (n) => schema.types.get(n),
        );
      } else if (ref.nonNull) {
        throw new GraphQLError(
          `Variable "$${name}" of required type "${typeRefToString(ref)}" was not provided`,
          'VARIABLE_TYPE',
        );
      } else {
        variables[name] = null;
      }
      continue;
    }

    variables[name] = coerceVariable(provided, ref, (n) => schema.types.get(n));
  }
  return variables;
}

// ---- 执行阶段 ----

export async function executePrepared<TContext>(
  schema: GraphQLSchema<TContext>,
  appContext: TContext,
  prepared: PreparedOperation,
): Promise<ExecutionResult> {
  const ctx: ExecutionContext<TContext> = {
    schema,
    appContext,
    fragments: prepared.fragments,
    variables: prepared.coercedVariables,
    errors: [],
  };

  const rootType =
    prepared.operation.operation === 'query' ? schema.queryType : schema.mutationType;
  const rootFields = collectFields(rootType, prepared.operation.selectionSet, {
    fragments: prepared.fragments,
    variables: prepared.coercedVariables,
  });

  const data: Record<string, unknown> = {};
  let rootBubbled = false;

  if (prepared.operation.operation === 'mutation') {
    for (const [key, field] of rootFields) {
      try {
        data[key] = await executeField(ctx, rootType, null, field, []);
      } catch (err) {
        if (isBubble(err)) {
          delete data[key];
          rootBubbled = true;
        } else {
          throw err;
        }
      }
    }
  } else {
    const entries = [...rootFields.entries()];
    const outcomes = await Promise.all(
      entries.map(async ([key, field]) => {
        try {
          return { key, value: await executeField(ctx, rootType, null, field, []), bubbled: false };
        } catch (err) {
          if (isBubble(err)) return { key, value: null, bubbled: true };
          throw err;
        }
      }),
    );
    for (const outcome of outcomes) {
      if (outcome.bubbled) {
        delete data[outcome.key];
        rootBubbled = true;
      } else {
        data[outcome.key] = outcome.value;
      }
    }
  }

  const result: ExecutionResult = { data: rootBubbled ? null : data };
  if (ctx.errors.length > 0) result.errors = ctx.errors;
  return result;
}

export async function execute<TContext>(
  schema: GraphQLSchema<TContext>,
  appContext: TContext,
  source: string,
  options: PrepareOptions = {},
): Promise<ExecutionResult> {
  const prepared = prepareExecution(schema, source, options);
  return executePrepared(schema, appContext, prepared);
}

// ---- 字段执行 ----

async function executeField<TContext>(
  ctx: ExecutionContext<TContext>,
  parentType: ObjectType,
  source: unknown,
  collected: CollectedField,
  path: PathSegment[],
): Promise<unknown> {
  const fieldNode = collected.node;
  const key = fieldNode.alias ? fieldNode.alias.value : fieldNode.name.value;
  const fieldPath = [...path, key];
  const fieldDef = parentType.fields.get(fieldNode.name.value);
  if (!fieldDef) {
    // 校验阶段已拦截；走到这里属于内核与 schema 不一致
    throw new GraphQLError(
      `Cannot query field "${fieldNode.name.value}" on type "${parentType.name}"`,
      'INTERNAL',
      { path: fieldPath, locations: fieldNode.name.loc },
    );
  }

  let args: Record<string, unknown>;
  try {
    args = coerceArguments(ctx, fieldDef, fieldNode);
  } catch (err) {
    // 理论上校验已拦截；防御性地按字段失败处理，而不是让整个请求 500
    ctx.errors.push(resolverError(err, fieldNode, fieldPath));
    if (fieldDef.type.nonNull) throw NON_NULL_BUBBLE;
    return null;
  }

  const resolver = getResolver(ctx.schema, parentType.name, fieldNode.name.value);
  let raw: unknown;
  let upstreamError = false;
  try {
    raw = await resolver(source, args, ctx.appContext, {
      fieldName: fieldNode.name.value,
      responseKey: key,
      path,
    });
  } catch (err) {
    upstreamError = true;
    ctx.errors.push(resolverError(err, fieldNode, fieldPath));
    raw = null;
  }

  return completeValue(
    ctx,
    fieldDef.type,
    raw,
    collected.mergedSelectionSet,
    fieldPath,
    fieldNode,
    upstreamError,
  );
}

function coerceArguments<TContext>(
  ctx: ExecutionContext<TContext>,
  fieldDef: FieldDef,
  fieldNode: FieldNode,
): Record<string, unknown> {
  const args: Record<string, unknown> = {};
  const provided = new Map(fieldNode.arguments.map((a) => [a.name.value, a]));

  for (const [argName, argDef] of fieldDef.args) {
    const argumentNode = provided.get(argName);
    if (!argumentNode) {
      args[argName] = argDef.defaultValue ?? null;
      continue;
    }
    if (argumentNode.value.kind === 'NullValue') {
      args[argName] = null;
      continue;
    }
    args[argName] = coerceLiteral(
      argumentNode.value,
      argDef.type,
      ctx.variables,
      (n) => ctx.schema.types.get(n),
    );
  }
  return args;
}

// ---- 值完成：标量 / 对象 / 列表 + 非空冒泡 ----

/**
 * 把解析器返回的原始值按声明类型完成。
 * 可空性判定全部集中在这里：任何位置的失败要么在当前位置停下（返回 null），
 * 要么抛 NON_NULL_BUBBLE 继续沿类型树向上冒泡。
 */
async function completeValue<TContext>(
  ctx: ExecutionContext<TContext>,
  ref: TypeRef,
  raw: unknown,
  selectionSet: CollectedField['mergedSelectionSet'],
  path: PathSegment[],
  fieldNode: FieldNode,
  upstreamError: boolean,
): Promise<unknown> {
  // 1. null：非空位置冒泡；可空位置停下
  if (raw === null || raw === undefined) {
    if (ref.nonNull) {
      if (!upstreamError) {
        ctx.errors.push(
          new GraphQLError(
            `Cannot return null for non-nullable position at path "${formatPath(path)}" (declared type "${typeRefToString(ref)}")`,
            'NON_NULL_VIOLATION',
            { path, locations: fieldNode.name.loc },
          ),
        );
      }
      throw NON_NULL_BUBBLE;
    }
    return null;
  }

  if (ref.kind === 'LIST') {
    if (!Array.isArray(raw)) {
      ctx.errors.push(
        new GraphQLError(
          `Expected a list at path "${formatPath(path)}" but resolver returned ${typeof raw}`,
          'COERCION_FAILURE',
          { path, locations: fieldNode.name.loc },
        ),
      );
      if (ref.nonNull) throw NON_NULL_BUBBLE;
      return null;
    }
    try {
      return await Promise.all(
        raw.map((item, index) =>
          completeValue(ctx, ref.ofType, item, selectionSet, [...path, index], fieldNode, false),
        ),
      );
    } catch (err) {
      if (!isBubble(err)) throw err;
      // 元素非空失败：整个列表置空；列表自身非空则继续向上冒泡
      if (ref.nonNull) throw NON_NULL_BUBBLE;
      return null;
    }
  }

  const namedType = ctx.schema.types.get(ref.name);
  if (!namedType) {
    throw new GraphQLError(`Unknown type "${ref.name}"`, 'INTERNAL', { path });
  }

  if (namedType.kind === 'OBJECT') {
    try {
      return await completeObject(ctx, namedType, raw, selectionSet, path);
    } catch (err) {
      if (!isBubble(err)) throw err;
      // 子级非空失败：对象整体置空；对象自身非空则继续向上冒泡
      if (ref.nonNull) throw NON_NULL_BUBBLE;
      return null;
    }
  }

  // 标量 / 枚举：输出强制失败按字段失败处理
  try {
    return completeLeaf(namedType, raw, path, fieldNode);
  } catch (err) {
    if (!isBubble(err)) {
      const normalized = err instanceof GraphQLError
        ? err
        : new GraphQLError(String(err), 'INTERNAL', { path });
      ctx.errors.push(normalized);
    }
    if (ref.nonNull) throw NON_NULL_BUBBLE;
    return null;
  }
}

function formatPath(path: PathSegment[]): string {
  return path.map((segment) => String(segment)).join('.');
}

async function completeObject<TContext>(
  ctx: ExecutionContext<TContext>,
  objectType: ObjectType,
  source: unknown,
  selectionSet: CollectedField['mergedSelectionSet'],
  path: PathSegment[],
): Promise<Record<string, unknown>> {
  if (!selectionSet) {
    // 校验已拦截；防御性处理
    throw new GraphQLError(
      `Missing selection set for object type "${objectType.name}"`,
      'INTERNAL',
      { path },
    );
  }

  const fields = collectFields(objectType, selectionSet, {
    fragments: ctx.fragments,
    variables: ctx.variables,
  });

  const result: Record<string, unknown> = {};
  const entries = [...fields.entries()];
  const outcomes = await Promise.all(
    entries.map(async ([key, field]) => {
      try {
        return { key, value: await executeField(ctx, objectType, source, field, path), bubbled: false };
      } catch (err) {
        if (isBubble(err)) return { key, value: null, bubbled: true };
        throw err;
      }
    }),
  );

  for (const outcome of outcomes) {
    if (outcome.bubbled) delete result[outcome.key];
    else result[outcome.key] = outcome.value;
  }
  if (outcomes.some((o) => o.bubbled)) {
    throw NON_NULL_BUBBLE;
  }
  return result;
}

function completeLeaf(
  namedType: Exclude<NamedType, ObjectType>,
  raw: unknown,
  path: PathSegment[],
  fieldNode: FieldNode,
): unknown {
  if (namedType.kind === 'ENUM') {
    if (typeof raw !== 'string' || !namedType.values.has(raw)) {
      throw new GraphQLError(
        `Enum "${namedType.name}" cannot represent value ${JSON.stringify(raw)}`,
        'COERCION_FAILURE',
        { path, locations: fieldNode.name.loc },
      );
    }
    return raw;
  }
  let coerced: unknown;
  try {
    coerced = coerceOutputScalar(raw, namedType.name);
    namedType.assertOutput(coerced);
  } catch (err) {
    // 标量强制错误默认没有路径/位置，这里在字段原点补齐
    const message = err instanceof Error ? err.message : String(err);
    throw new GraphQLError(message, 'COERCION_FAILURE', {
      path,
      locations: fieldNode.name.loc,
    });
  }
  return coerced;
}

// ---- 错误构造 ----

function resolverError(err: unknown, fieldNode: FieldNode, path: PathSegment[]): GraphQLError {
  if (err instanceof GraphQLError) {
    return new GraphQLError(err.message, err.category, {
      path,
      locations: fieldNode.name.loc,
      extra: err.extra,
    });
  }
  const message = err instanceof Error ? err.message : String(err);
  return new GraphQLError(message, 'RESOLVER_FAILURE', {
    path,
    locations: fieldNode.name.loc,
  });
}
