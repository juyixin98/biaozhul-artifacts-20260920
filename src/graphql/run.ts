/**
 * 请求编排：parse -> 选择 operation -> validate -> 变量强制转换 -> execute。
 * 输出决策(accepted/rejected/indeterminate)与原因码，供诊断层记录。
 */

import type {
  DocumentNode,
  OperationDefinitionNode,
} from './ast.js';
import { parse } from './parser.js';
import { validateDocument } from './validate.js';
import { coerceVariableValues } from './values.js';
import { executeOperation, type ExecutionResult } from './execute.js';
import { GraphQLError, toGraphQLError } from './error.js';
import type { FragmentMap } from './collect.js';
import type { GraphQLContext, GraphQLSchema } from './schema.js';

export type Decision = 'accepted' | 'rejected' | 'indeterminate';

export interface RunOutcome {
  result: ExecutionResult;
  decision: Decision;
  /** 机器可读原因码，例如 PARSE_SYNTAX / VALIDATION_FRAGMENT_CYCLE / VARIABLE_COERCION */
  reasons: string[];
  document: DocumentNode | null;
  operation: OperationDefinitionNode | null;
  resolverErrorCount: number;
}

export interface RunInput {
  schema: GraphQLSchema;
  query: string;
  variables?: Record<string, unknown> | null;
  operationName?: string | null;
  context: GraphQLContext;
}

export async function runGraphQL(input: RunInput): Promise<RunOutcome> {
  const { schema, context } = input;

  let document: DocumentNode;
  try {
    document = parse(input.query);
  } catch (error) {
    const gqlError = toGraphQLError(error, 'Parse failed');
    if (!(error instanceof GraphQLError)) {
      gqlError.extensions.category = 'PARSE';
    }
    return rejected({
      result: { errors: [normalize(gqlError)] },
      reasons: ['PARSE_SYNTAX'],
      document: null,
      operation: null,
    });
  }

  const operations = document.definitions.filter(
    (def): def is OperationDefinitionNode => def.kind === 'OperationDefinition',
  );
  const fragments: FragmentMap = {};
  for (const def of document.definitions) {
    if (def.kind === 'FragmentDefinition') fragments[def.name.value] = def;
  }

  const selected = selectOperation(operations, input.operationName ?? null);
  if (selected.error) {
    // 多操作歧义时无法判定执行目标；无操作/操作名未知是明确的客户端错误。
    const decision = selected.reason === 'OPERATION_AMBIGUOUS' ? 'indeterminate' : 'rejected';
    return rejected({
      result: { errors: [selected.error] },
      reasons: [selected.reason],
      document,
      operation: null,
      decision,
    });
  }
  const operation = selected.operation;

  const validationErrors = validateDocument(schema, document);
  if (validationErrors.length > 0) {
    return rejected({
      result: { errors: validationErrors.map(normalize) },
      reasons: summarizeReasons(validationErrors),
      document,
      operation,
    });
  }

  const { values: coercedVariables, errors: coercionErrors } = coerceVariableValues(
    schema,
    operation.variableDefinitions,
    input.variables ?? {},
  );
  if (coercionErrors.length > 0) {
    return rejected({
      result: { errors: coercionErrors.map(normalize) },
      reasons: ['VARIABLE_COERCION'],
      document,
      operation,
    });
  }

  const result = await executeOperation({
    schema,
    operation,
    fragments,
    variables: coercedVariables,
    context,
  });

  return {
    result,
    decision: 'accepted',
    reasons: result.errors && result.errors.length > 0 ? ['EXECUTED_WITH_FIELD_ERRORS'] : ['EXECUTED'],
    document,
    operation,
    resolverErrorCount: result.errors?.length ?? 0,
  };
}

function selectOperation(
  operations: OperationDefinitionNode[],
  operationName: string | null,
):
  | { operation: OperationDefinitionNode; error: null; reason?: never }
  | { operation: null; error: GraphQLError; reason: string } {
  if (operations.length === 0) {
    return {
      operation: null,
      error: new GraphQLError('The document does not contain any operation.', {
        category: 'VALIDATION',
      }),
      reason: 'NO_OPERATION',
    };
  }
  if (operationName === null) {
    if (operations.length === 1) {
      return { operation: operations[0], error: null };
    }
    return {
      operation: null,
      error: new GraphQLError(
        'Must provide operation name when the document contains multiple operations.',
        { category: 'VALIDATION' },
      ),
      reason: 'OPERATION_AMBIGUOUS',
    };
  }
  const match = operations.find((op) => op.name?.value === operationName);
  if (!match) {
    return {
      operation: null,
      error: new GraphQLError(`Unknown operation named "${operationName}".`, {
        category: 'VALIDATION',
      }),
      reason: 'OPERATION_UNKNOWN',
    };
  }
  return { operation: match, error: null };
}

function summarizeReasons(errors: GraphQLError[]): string[] {
  const reasons = new Set<string>();
  for (const error of errors) {
    if (error.message.includes('within itself')) reasons.add('VALIDATION_FRAGMENT_CYCLE');
    else if (error.message.includes('conflict')) reasons.add('VALIDATION_FIELD_CONFLICT');
    else if (error.message.includes('Variable')) reasons.add('VALIDATION_VARIABLE_USAGE');
    else reasons.add('VALIDATION_SCHEMA');
  }
  return [...reasons];
}

function normalize(error: GraphQLError): GraphQLError {
  return error;
}

function rejected(params: {
  result: ExecutionResult;
  reasons: string[];
  document: DocumentNode | null;
  operation: OperationDefinitionNode | null;
  decision?: Decision;
}): RunOutcome {
  return {
    result: params.result,
    decision: params.decision ?? 'rejected',
    reasons: params.reasons,
    document: params.document,
    operation: params.operation,
    resolverErrorCount: 0,
  };
}
