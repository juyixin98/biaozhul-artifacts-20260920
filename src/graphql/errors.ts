/**
 * 统一错误模型。
 * category 用于区分"失败类别"——验收要求不能把所有失败都变成 500，
 * HTTP 层依据 category 决定状态码与可判定性。
 */
import type { Location } from './ast.js';

export type ErrorCategory =
  | 'SYNTAX'
  | 'VALIDATION'
  | 'VARIABLE_TYPE'
  | 'FRAGMENT_CYCLE'
  | 'FRAGMENT_NOT_FOUND'
  | 'FIELD_CONFLICT'
  | 'FIELD_NOT_FOUND'
  | 'ARGUMENT'
  | 'DIRECTIVE'
  | 'AMBIGUOUS_OPERATION'
  | 'UNKNOWN_OPERATION'
  | 'NON_NULL_VIOLATION'
  | 'COERCION_FAILURE'
  | 'RESOLVER_FAILURE'
  | 'BAD_REQUEST'
  | 'INTERNAL';

export interface GraphQLErrorShape {
  message: string;
  path?: ReadonlyArray<string | number>;
  locations?: ReadonlyArray<{ line: number; column: number }>;
  extensions: {
    category: ErrorCategory;
    [key: string]: unknown;
  };
}

export class GraphQLError extends Error {
  readonly path?: ReadonlyArray<string | number>;
  readonly category: ErrorCategory;
  readonly locations?: ReadonlyArray<{ line: number; column: number }>;
  readonly extra?: Record<string, unknown>;

  get extensions(): { category: ErrorCategory; [key: string]: unknown } {
    return { category: this.category, ...this.extra };
  }

  constructor(
    message: string,
    category: ErrorCategory,
    options: {
      path?: ReadonlyArray<string | number>;
      locations?: Location | ReadonlyArray<Location>;
      extra?: Record<string, unknown>;
    } = {},
  ) {
    super(message);
    this.name = 'GraphQLError';
    this.category = category;
    this.path = options.path;
    this.extra = options.extra;
    if (options.locations) {
      const locs = Array.isArray(options.locations) ? options.locations : [options.locations];
      this.locations = locs.map((l) => ({ line: l.line, column: l.column }));
    }
  }

  toJSON(): GraphQLErrorShape {
    const shape: GraphQLErrorShape = {
      message: this.message,
      extensions: { category: this.category, ...this.extra },
    };
    if (this.path && this.path.length > 0) shape.path = [...this.path];
    if (this.locations && this.locations.length > 0) shape.locations = this.locations;
    return shape;
  }
}

/** 内部信号：非空位置得到 null，沿类型树向上冒泡。附带的错误已在原点记录。 */
export const NON_NULL_BUBBLE: unique symbol = Symbol('graphql.nonNullBubble');

export function isBubble(err: unknown): boolean {
  return err === NON_NULL_BUBBLE;
}
