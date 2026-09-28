/**
 * 统一错误类型与非空冒泡控制信号。
 * 失败类别(category)用于测试精确断言，而不是只看 HTTP 500。
 */

export type ErrorCategory =
  | 'PARSE'
  | 'VALIDATION'
  | 'COERCION'
  | 'RESOLVER'
  | 'INTERNAL';

export type ErrorPathElement = string | number;

export interface SourceLocation {
  line: number;
  column: number;
}

export interface GraphQLErrorOptions {
  path?: ReadonlyArray<ErrorPathElement>;
  locations?: SourceLocation[];
  category?: ErrorCategory;
  extensions?: Record<string, unknown>;
}

export class GraphQLError extends Error {
  readonly path?: ReadonlyArray<ErrorPathElement>;
  readonly locations?: SourceLocation[];
  readonly extensions: Record<string, unknown>;

  constructor(message: string, options: GraphQLErrorOptions = {}) {
    super(message);
    this.name = 'GraphQLError';
    if (options.path) this.path = [...options.path];
    if (options.locations && options.locations.length > 0) {
      this.locations = options.locations;
    }
    this.extensions = {
      category: options.category ?? 'INTERNAL',
      ...(options.extensions ?? {}),
    };
  }

  get category(): ErrorCategory {
    return this.extensions.category as ErrorCategory;
  }

  toFormattedError(): {
    message: string;
    path?: ErrorPathElement[];
    locations?: SourceLocation[];
    extensions: Record<string, unknown>;
  } {
    return {
      message: this.message,
      ...(this.path ? { path: [...this.path] } : {}),
      ...(this.locations ? { locations: this.locations } : {}),
      extensions: this.extensions,
    };
  }
}

/**
 * 非空边界冒泡信号：携带“原始错误”，沿类型树上抛，
 * 直到遇到可空边界才落入 errors（原始错误只记录一次）。
 */
export class NonNullViolation extends Error {
  readonly original: GraphQLError;

  constructor(original: GraphQLError) {
    super(`Non-null violation caused by: ${original.message}`);
    this.name = 'NonNullViolation';
    this.original = original;
  }
}

export function toGraphQLError(
  error: unknown,
  fallbackMessage = 'Unexpected error',
): GraphQLError {
  if (error instanceof GraphQLError) return error;
  if (error instanceof Error) {
    return new GraphQLError(error.message || fallbackMessage, {
      category: 'INTERNAL',
    });
  }
  return new GraphQLError(fallbackMessage, { category: 'INTERNAL' });
}
