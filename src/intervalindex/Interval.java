package intervalindex;

/**
 * 半开区间 [lo, hi)：端点为 long 整数，左闭右开。
 *
 * @param lo 左端点（包含）
 * @param hi 右端点（不包含）
 */
public record Interval(long lo, long hi) {

    /** 校验并构造区间；空区间（lo == hi）与逆序区间（lo > hi）一律拒绝。 */
    public static Interval of(long lo, long hi) {
        if (lo >= hi) {
            throw new IllegalArgumentException(
                    "invalid interval [%d, %d): lo must be < hi; empty and reversed intervals are rejected"
                            .formatted(lo, hi));
        }
        return new Interval(lo, hi);
    }
}
