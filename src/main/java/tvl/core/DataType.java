package tvl.core;

/**
 * 表达式支持的数据类型。
 *
 * SQL 三值逻辑中 NULL 表示“未知”，它既是一种取值，也在这里被建模为一种
 * 类型标记：未定型的 NULL 字面量在类型检查阶段会尝试与另一侧操作数对齐。
 */
public enum DataType {
    NULL,
    INTEGER,
    BOOLEAN,
    STRING;

    /** 整数算术运算只接受 INTEGER（NULL 由调用方先行传播）。 */
    public boolean isInteger() {
        return this == INTEGER;
    }

    /** =、&lt;&gt;、IS NULL 之外的排序比较只支持 INTEGER 与 STRING。 */
    public boolean isOrderable() {
        return this == INTEGER || this == STRING;
    }
}
