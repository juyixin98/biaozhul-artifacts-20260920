/**
 * GraphQL-over-HTTP 处理层：请求信封校验、错误类别 -> HTTP 状态映射、
 * 诊断记录装配。执行内核的结果原样以 GraphQL 响应信封返回。
 *
 * 状态码约定（刻意区分，不把一切变成 500）：
 * - 200：执行完成；可能带 errors + 部分数据，或 data 为 null（根级非空冒泡）
 * - 400：信封错误 / 语法 / 执行前校验（含变量类型、片段循环、字段合并冲突）
 * - 404：指定的 operationName 不存在（UNKNOWN_OPERATION）
 * - 405：错误的 HTTP 方法
 * - 500：内核自身的未预期缺陷（正常夹具下不应出现）
 */
import type { FastifyReply, FastifyRequest } from 'fastify';
import { GraphQLError } from '../graphql/errors.js';
import { executePrepared, prepareExecution, ValidationFailure, type ExecutionResult } from '../graphql/executor.js';
import type { GraphQLSchema } from '../graphql/schema.js';
import { type DiagnosticLogger, type Decision } from '../diagnostics/logger.js';
import { shapeOfVariables } from '../diagnostics/redact.js';
import type { AppContext } from '../resolvers.js';

interface GraphQLRequestEnvelope {
  query?: unknown;
  variables?: unknown;
  operationName?: unknown;
}

const BAD_REQUEST_CATEGORIES = new Set([
  'SYNTAX',
  'VALIDATION',
  'VARIABLE_TYPE',
  'FRAGMENT_CYCLE',
  'FRAGMENT_NOT_FOUND',
  'FIELD_CONFLICT',
  'FIELD_NOT_FOUND',
  'ARGUMENT',
  'DIRECTIVE',
  'AMBIGUOUS_OPERATION',
  'BAD_REQUEST',
]);

export interface HandlerDeps {
  schema: GraphQLSchema<AppContext>;
  createContext: (requestId: string) => AppContext;
  diagnostics: DiagnosticLogger;
}

function errorStatusCode(err: GraphQLError): number {
  if (err.category === 'UNKNOWN_OPERATION') return 404;
  if (BAD_REQUEST_CATEGORIES.has(err.category)) return 400;
  return 500;
}

function errorSummary(errors: GraphQLError[]): string {
  if (errors.length === 0) return 'OK';
  const first = errors[0]!;
  if (errors.length === 1) return first.message;
  return `${first.message} (+${errors.length - 1} more)`;
}

function toGraphQLPayload(err: GraphQLError): { data: null; errors: ReturnType<GraphQLError['toJSON']>[] } {
  return { data: null, errors: [err.toJSON()] };
}

function toValidationPayload(
  failure: ValidationFailure,
): { data: null; errors: ReturnType<GraphQLError['toJSON']>[] } {
  return { data: null, errors: failure.validationErrors.map((e) => e.toJSON()) };
}

export function createGraphqlHandler(deps: HandlerDeps) {
  return async (request: FastifyRequest, reply: FastifyReply): Promise<void> => {
    const requestId = (request.id as string) ?? cryptoRandomRequestId();
    reply.header('x-request-id', requestId);
    const started = Date.now();

    let envelope: GraphQLRequestEnvelope;
    try {
      envelope = readEnvelope(request);
    } catch (err) {
      const gqlError =
        err instanceof GraphQLError
          ? err
          : new GraphQLError(
              err instanceof Error ? err.message : 'Malformed GraphQL request',
              'BAD_REQUEST',
            );
      recordDiagnostic(deps.diagnostics, {
        requestId,
        started,
        decision: 'UNDECIDABLE',
        phase: 'envelope',
        reasons: [gqlError.category],
        summary: gqlError.message,
        operationName: null,
        operationType: null,
        variables: {},
        fieldErrorCount: 0,
        httpStatus: 400,
      });
      await reply.code(400).send(toGraphQLPayload(gqlError));
      return;
    }

    const operationName =
      typeof envelope.operationName === 'string' ? envelope.operationName : null;
    const rawVariables = envelope.variables;

    // 准备阶段：解析 + 校验 + 变量强制。任一失败都拒绝执行。
    let prepared: ReturnType<typeof prepareExecution>;
    try {
      prepared = prepareExecution(deps.schema, String(envelope.query), {
        operationName,
        variables: rawVariables,
      });
    } catch (err) {
      if (err instanceof ValidationFailure) {
        const status = Math.max(
          ...err.validationErrors.map((e) => errorStatusCode(e)),
        );
        const httpStatus = status === 500 ? 400 : status;
        recordDiagnostic(deps.diagnostics, {
          requestId,
          started,
          decision: 'REJECTED',
          phase: err.validationErrors.some((e) => e.category === 'SYNTAX')
            ? 'parse'
            : 'validate',
          reasons: [...new Set(err.validationErrors.map((e) => e.category))],
          summary: errorSummary(err.validationErrors),
          operationName,
          operationType: null,
          variables: {},
          fieldErrorCount: 0,
          httpStatus,
        });
        await reply.code(httpStatus).send(toValidationPayload(err));
        return;
      }
      if (err instanceof GraphQLError) {
        const httpStatus = errorStatusCode(err);
        recordDiagnostic(deps.diagnostics, {
          requestId,
          started,
          decision: 'REJECTED',
          phase:
            err.category === 'SYNTAX'
              ? 'parse'
              : err.category === 'VARIABLE_TYPE'
                ? 'coerce-variables'
                : 'validate',
          reasons: [err.category],
          summary: err.message,
          operationName,
          operationType: null,
          variables: {},
          fieldErrorCount: 0,
          httpStatus,
        });
        await reply.code(httpStatus).send(toGraphQLPayload(err));
        return;
      }
      throw err;
    }

    const appContext = deps.createContext(requestId);
    let result: ExecutionResult;
    try {
      result = await executePrepared(deps.schema, appContext, prepared);
    } catch (err) {
      // 走到这里说明准备与执行之间出现了未预期分歧，属于内核缺陷信号
      const message = err instanceof Error ? err.message : String(err);
      const internal = new GraphQLError(`Internal execution failure: ${message}`, 'INTERNAL');
      request.log.error({ requestId, err }, 'unexpected executor failure');
      recordDiagnostic(deps.diagnostics, {
        requestId,
        started,
        decision: 'REJECTED',
        phase: 'fatal',
        reasons: ['INTERNAL'],
        summary: internal.message,
        operationName,
        operationType: prepared.operation.operation,
        variables: prepared.coercedVariables,
        fieldErrorCount: 0,
        httpStatus: 500,
      });
      await reply.code(500).send(toGraphQLPayload(internal));
      return;
    }

    const fieldErrors = result.errors ?? [];
    recordDiagnostic(deps.diagnostics, {
      requestId,
      started,
      decision: 'ACCEPTED',
      phase: 'execute',
      reasons: fieldErrors.length > 0 ? fieldErrors.map((e) => e.category) : ['OK'],
      summary:
        fieldErrors.length === 0
          ? 'executed successfully'
          : `executed with ${fieldErrors.length} field error(s); data=${result.data === null ? 'null' : 'partial-or-complete'}`,
      operationName: prepared.operation.name?.value ?? null,
      operationType: prepared.operation.operation,
      variables: prepared.coercedVariables,
      fieldErrorCount: fieldErrors.length,
      httpStatus: 200,
    });

    await reply.code(200).send(result);
  };
}

function readEnvelope(request: FastifyRequest): GraphQLRequestEnvelope {
  if (request.method === 'GET') {
    const query = request.query as Record<string, unknown>;
    let variables: unknown;
    if (typeof query.variables === 'string' && query.variables.length > 0) {
      try {
        variables = JSON.parse(query.variables) as unknown;
      } catch {
        throw new GraphQLError('Query parameter "variables" must be valid JSON', 'BAD_REQUEST');
      }
    }
    return {
      query: query.query,
      variables,
      operationName: query.operationName,
    };
  }

  const body = request.body as unknown;
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    throw new GraphQLError('Request body must be a JSON object', 'BAD_REQUEST');
  }
  const envelope = body as Record<string, unknown>;
  if (typeof envelope.query !== 'string' || envelope.query.trim().length === 0) {
    throw new GraphQLError('Request body must contain a non-empty "query" string', 'BAD_REQUEST');
  }
  if (
    envelope.variables !== undefined &&
    envelope.variables !== null &&
    (typeof envelope.variables !== 'object' || Array.isArray(envelope.variables))
  ) {
    throw new GraphQLError('"variables" must be an object when provided', 'BAD_REQUEST');
  }
  if (
    envelope.operationName !== undefined &&
    envelope.operationName !== null &&
    typeof envelope.operationName !== 'string'
  ) {
    throw new GraphQLError('"operationName" must be a string when provided', 'BAD_REQUEST');
  }
  return {
    query: envelope.query,
    variables: envelope.variables,
    operationName: envelope.operationName,
  };
}

interface RecordArgs {
  requestId: string;
  started: number;
  decision: Decision;
  phase: 'envelope' | 'parse' | 'validate' | 'coerce-variables' | 'execute' | 'fatal';
  reasons: string[];
  summary: string;
  operationName: string | null;
  operationType: 'query' | 'mutation' | null;
  variables: Record<string, unknown>;
  fieldErrorCount: number;
  httpStatus: number;
}

function recordDiagnostic(logger: DiagnosticLogger, args: RecordArgs): void {
  logger.record({
    requestId: args.requestId,
    timestamp: new Date().toISOString(),
    durationMs: Date.now() - args.started,
    decision: args.decision,
    phase: args.phase,
    reasons: args.reasons,
    summary: args.summary,
    operationName: args.operationName,
    operationType: args.operationType,
    variableShapes: shapeOfVariables(args.variables),
    fieldErrorCount: args.fieldErrorCount,
    httpStatus: args.httpStatus,
  });
}

function cryptoRandomRequestId(): string {
  // 全局 crypto 在 Node 22 上默认可用
  return globalThis.crypto.randomUUID();
}
