/**
 * GraphQL 受限内核的公共入口：契约解析、校验、执行、类型与错误。
 * HTTP 与状态适配不从此文件导出，保持内核边界清晰。
 */
export * from './ast.js';
export * from './errors.js';
export * from './types.js';
export { parse, ParseError } from './parser.js';
export { Lexer, LexerError, TokenKind } from './lexer.js';
export type { Token } from './lexer.js';
export {
  coerceLiteral,
  coerceOutputScalar,
  coerceVariable,
  typeNodeToRef,
  typeNodeToString,
} from './coercion.js';
export { parseTypeRef, typeRefToString } from './types.js';
export { validateDocument } from './validator.js';
export { collectFields, responseKey, shouldIncludeNode } from './collect.js';
export type { CollectedField } from './collect.js';
export {
  execute,
  executePrepared,
  prepareExecution,
  ValidationFailure,
} from './executor.js';
export type { ExecutionResult, PreparedOperation } from './executor.js';
export {
  buildSchema,
  makeSchema,
  getObjectType,
  getResolver,
} from './schema.js';
export type {
  FieldResolveInfo,
  FieldResolver,
  GraphQLSchema,
  SchemaDefinition,
  SchemaSpec,
  SchemaTypeView,
} from './schema.js';
