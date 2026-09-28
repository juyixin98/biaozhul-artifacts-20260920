/**
 * 诊断脱敏。
 * 规则：键名命中敏感集合（含 schema 中显式标记的 sensitive 字段/参数名）
 * 时，值一律替换为掩码，且不保留长度、前缀等可推断信息。
 * 对数组递归处理；对象键本身不视为敏感数据。
 */

/**
 * 敏感键判定：归一化（去分隔符、小写）后命中敏感词，
 * 或按 camelCase 分段后存在敏感词段。
 * 不使用裸 "auth" 以免误伤 "author" 之类的普通键。
 */
const SENSITIVE_TOKENS = [
  'email',
  'password',
  'passwd',
  'pwd',
  'token',
  'secret',
  'authorization',
  'apikey',
  'credential',
  'credentials',
];

const MASK = '***REDACTED***';

function normalize(key: string): string {
  return key.replace(/[_\-\s]/g, '').toLowerCase();
}

export function isSensitiveKey(key: string): boolean {
  const flat = normalize(key);
  if (SENSITIVE_TOKENS.includes(flat)) return true;
  // camelCase 分段：userEmail -> ["user", "email"]
  const segments = key
    .split(/(?=[A-Z])/)
    .map((part) => normalize(part))
    .filter((part) => part.length > 0);
  if (segments.some((part) => SENSITIVE_TOKENS.includes(part))) return true;
  // 复合词：emailAddress、xTokenValue、apiKeyValue 等
  return SENSITIVE_TOKENS.some((token) => flat.includes(token));
}

export function redactValue(value: unknown, seen: WeakSet<object> = new WeakSet()): unknown {
  if (value === null || typeof value !== 'object') return value;
  if (seen.has(value as object)) return '[Circular]';
  seen.add(value as object);

  if (Array.isArray(value)) {
    return value.map((item) => redactValue(item, seen));
  }

  const result: Record<string, unknown> = {};
  for (const [key, raw] of Object.entries(value as Record<string, unknown>)) {
    if (isSensitiveKey(key)) {
      result[key] = MASK;
    } else {
      result[key] = redactValue(raw, seen);
    }
  }
  return result;
}

/** 用于日志的变量概览：名称 -> 类型/值形态，不打印具体内容 */
export function shapeOfVariables(variables: Record<string, unknown>): Record<string, string> {
  const shape: Record<string, string> = {};
  for (const [key, value] of Object.entries(variables)) {
    if (value === null) {
      shape[key] = 'null';
    } else if (Array.isArray(value)) {
      shape[key] = `list[${value.length}]`;
    } else {
      shape[key] = typeof value;
    }
  }
  return shape;
}
