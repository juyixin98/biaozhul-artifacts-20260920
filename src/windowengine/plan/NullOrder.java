package windowengine.plan;

/**
 * NULL 排序位置：FIRST 排分区最前，LAST 排分区最后。
 * 若请求中不指定，遵循 SQL 标准默认：ASC 时 NULLS FIRST，DESC 时 NULLS LAST。
 */
public enum NullOrder {
    FIRST, LAST;

    public static NullOrder parse(String raw) {
        try {
            return valueOf(raw.toUpperCase());
        } catch (IllegalArgumentException e) {
            throw new windowengine.EngineException(windowengine.ErrorCode.INVALID_REQUEST,
                    "非法 NULL 顺序: " + raw + "（只支持 FIRST / LAST）");
        }
    }

    public static NullOrder defaultValue(Direction direction) {
        return direction == Direction.ASC ? FIRST : LAST;
    }
}
