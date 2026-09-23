package windowengine.plan;

/** 窗口函数种类：本引擎实现 ROW_NUMBER、RANK 以及聚合 SUM。 */
public enum WindowFunction {
    ROW_NUMBER,
    RANK,
    SUM;

    public static WindowFunction parse(String raw) {
        return switch (raw.toUpperCase()) {
            case "ROW_NUMBER", "ROWNUMBER" -> ROW_NUMBER;
            case "RANK" -> RANK;
            case "SUM" -> SUM;
            default -> throw new windowengine.EngineException(windowengine.ErrorCode.UNSUPPORTED,
                    "不支持的窗口函数: " + raw + "（支持 ROW_NUMBER / RANK / SUM）");
        };
    }

    /** ROW_NUMBER / RANK 的结果恒为整数；SUM 在 NULL 值情况下可能产生 NULL。 */
    public windowengine.Value.Type resultType() {
        return windowengine.Value.Type.LONG;
    }
}
