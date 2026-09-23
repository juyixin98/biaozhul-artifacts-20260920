package tvl.evaluator;

/** 可按列名取值的数据行（由引擎的 Row 实现）。 */
public interface RowLike {
    /** 返回 Long / String / Boolean 或 null（SQL NULL）。 */
    Object get(String column);
}
