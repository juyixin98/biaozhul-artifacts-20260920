/**
 * 状态适配契约：执行内核不直接依赖 SQLite。
 * 适配器负责把“父行 + 字段 + 参数”解析为标量、单行或行迭代器。
 *
 * 列表刻意暴露为 Iterable 而非数组：预算在元素粒度扣减，
 * 超限时执行器停止迭代，未完成的元素解析器随之取消。
 */
import type { FieldDef } from '../contract/types.js';

export type Row = Record<string, unknown>;

export interface ResolveContext {
  parent: Row | null;
  typeName: string;
  field: FieldDef;
  args: Record<string, unknown>;
  path: string;
}

export interface StateAdapter {
  /** 根查询行（Query 类型的各字段从这里解析） */
  root(): Row;
  /** 标量字段 */
  resolveScalar(ctx: ResolveContext): unknown;
  /** 单对象字段 */
  resolveObject(ctx: ResolveContext): Row | null;
  /** 列表字段：惰性迭代，元素数即【真实基数】，可能高于声明上界 */
  iterateList(ctx: ResolveContext): Iterable<Row>;
}

export class AdapterError extends Error {
  constructor(
    message: string,
    readonly causeDetail?: unknown,
  ) {
    super(message);
    this.name = 'AdapterError';
  }
}
