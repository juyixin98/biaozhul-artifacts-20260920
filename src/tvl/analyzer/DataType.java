package tvl.analyzer;

/** 列/表达式的静态类型。NULL 字面量没有确定类型，用 {@link #NULL} 表示。 */
public enum DataType {
    INTEGER,
    STRING,
    BOOLEAN,
    NULL;

    public String displayName() {
        return switch (this) {
            case INTEGER -> "INTEGER";
            case STRING -> "STRING";
            case BOOLEAN -> "BOOLEAN";
            case NULL -> "NULL";
        };
    }
}
