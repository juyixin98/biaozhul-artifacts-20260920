/**
 * Tiny request-validation helper (deliberately minimal; no external zod dep).
 * `parse` throws a Fastify-compatible validation error carrying the path.
 */
import { AppError } from './errors.js';

interface V<T> {
  parse(input: unknown, path?: string): T;
  safeParse(input: unknown): { value?: T; error?: string };
}

function fail(path: string, expected: string, got: unknown): never {
  throw new AppError(
    'E_VALIDATION',
    `${path || 'body'}: expected ${expected}, got ${got === null ? 'null' : typeof got}`,
    400,
  );
}

function make<T>(check: (input: unknown, path: string) => T): V<T> {
  return {
    parse(input, path = 'body') {
      return check(input, path);
    },
    safeParse(input) {
      try {
        return { value: check(input, 'query') };
      } catch (e) {
        return { error: (e as Error).message };
      }
    },
  };
}

export const z = {
  string(): V<string> {
    return make((v, p) => (typeof v === 'string' ? v : fail(p, 'string', v)));
  },
  intNonneg(): V<number> {
    return make((v, p) =>
      typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : fail(p, 'non-negative integer', v),
    );
  },
  intInRange(min: number, max: number, fallback: number): V<number> {
    return make((v, p) => {
      if (v === undefined || v === null || v === '') return fallback;
      const n = typeof v === 'string' ? Number(v) : v;
      return typeof n === 'number' && Number.isInteger(n) && n >= min && n <= max
        ? n
        : fail(p, `integer in [${min}, ${max}]`, v);
    });
  },
  regex(re: RegExp, message: string): V<string> {
    return make((v, p) => (typeof v === 'string' && re.test(v) ? v : fail(p, message, v)));
  },
  object<S extends Record<string, V<unknown>>>(shape: S): V<{ [K in keyof S]: ReturnType<S[K]['parse']> }> {
    return make((input, p) => {
      if (typeof input !== 'object' || input === null || Array.isArray(input)) fail(p, 'object', input);
      const out: Record<string, unknown> = {};
      for (const [key, validator] of Object.entries(shape)) {
        out[key] = (validator as V<unknown>).parse((input as Record<string, unknown>)[key], `${p}.${key}`);
      }
      return out as never;
    });
  },
};
