package windowengine.plan;

/**
 * 排序方向：ASC 升序 / DESC 降序。
 */
public enum Direction {
    ASC, DESC;

    public static Direction parse(String raw) {
        try {
            return valueOf(raw.toUpperCase());
        } catch (IllegalArgumentException e) {
            throw new windowengine.EngineException(windowengine.ErrorCode.INVALID_REQUEST,
                    "非法排序方向: " + raw + "（只支持 ASC / DESC）");
        }
    }
}
