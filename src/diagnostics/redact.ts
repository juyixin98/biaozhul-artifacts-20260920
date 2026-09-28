/**
 * 脱敏：诊断输出只允许出现脱敏后的变量与文本。
 *  - 变量：默认只保留键与类型形状，值一律打码（变量可能携带敏感输入）。
 *  - 文本：邮箱局部打码；password/token/secret 等键的值打码。
 */

const SENSITIVE_KEY = /(password|passwd|secret|token|apikey|api_key|authorization|cookie)/i;
const EMAIL_PATTERN = /([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})/g;

export function redactText(text: string): string {
  return text.replace(EMAIL_PATTERN, (_match, first: string, domain: string) => {
    return `${first}***${domain}`;
  });
}

export function redactVariables(
  variables: Record<string, unknown> | null | undefined,
): Record<string, string> {
  const out: Record<string, string> = {};
  if (!variables) return out;
  for (const [key, value] of Object.entries(variables)) {
    if (SENSITIVE_KEY.test(key) || key.toLowerCase() === 'email') {
      out[key] = '<redacted>';
    } else {
      out[key] = shapeOnly(value);
    }
  }
  return out;
}

function shapeOnly(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return `array[${value.length}]`;
  const type = typeof value;
  if (type === 'object') {
    return `object{${Object.keys(value as Record<string, unknown>).join(',')}}`;
  }
  return type;
}

export function redactErrorObject(error: {
  message: string;
  path?: ReadonlyArray<string | number>;
  locations?: unknown;
  extensions?: Record<string, unknown>;
}): {
  message: string;
  path?: ReadonlyArray<string | number>;
  locations?: unknown;
  extensions?: Record<string, unknown>;
} {
  return {
    ...error,
    message: redactText(error.message),
  };
}
