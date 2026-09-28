/**
 * 状态适配层契约。
 *
 * 执行内核只依赖 EntityStore 抽象，不直接碰 SQL：
 * - load：按主键取单个对象；
 * - resolveList：沿关系取有序子对象，支持 eq 过滤变量。
 * 适配器抛出的任何异常在引擎边界被转译为 COMPUTATION_FAILED，
 * 而“对象不存在”通过返回 null 表达为 STATE_CONFLICT。
 */
export type ScalarColumn = string | number | boolean | null;

export interface EntityRecord {
  id: number;
  [column: string]: ScalarColumn;
}

export interface ListFilter {
  /** 子对象上的过滤列。 */
  column: string;
  value: string | number | boolean;
}

export interface EntityStore {
  load(typeName: string, id: number): Promise<EntityRecord | null>;
  resolveList(
    relation: string,
    parentType: string,
    parentId: number,
    childType: string,
    filters: ListFilter[],
  ): Promise<EntityRecord[]>;
  close(): Promise<void>;
}
