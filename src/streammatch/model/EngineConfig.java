package streammatch.model;

/**
 * 引擎配置（运行期可通过服务层重新配置）。
 *
 * @param mode           事件时间 / 处理时间
 * @param windowMillis   时间窗口 W（毫秒，必须 &gt; 0）。A 在 t 发生，则 B 的时间 tB 必须满足
 *                       {@code 0 <= tB - t <= W} 才算在时限内（区间两端均为闭区间）。
 * @param matchPolicy    重叠匹配策略，见 {@link MatchPolicy}
 * @param allowedLateness 允许迟到毫秒数 L（仅事件时间模式，{@code >= 0}）。
 *                       watermark = 已见最大事件时间 - L。
 * @param latePolicy     迟到事件处理策略，见 {@link LatePolicy}（仅事件时间模式）
 */
public record EngineConfig(EngineMode mode,
                           long windowMillis,
                           MatchPolicy matchPolicy,
                           long allowedLateness,
                           LatePolicy latePolicy) {

    public EngineConfig {
        if (mode == null) {
            throw new IllegalArgumentException("mode must not be null");
        }
        if (windowMillis <= 0) {
            throw new IllegalArgumentException("windowMillis must be > 0, got " + windowMillis);
        }
        if (matchPolicy == null) {
            throw new IllegalArgumentException("matchPolicy must not be null");
        }
        if (allowedLateness < 0) {
            throw new IllegalArgumentException("allowedLateness must be >= 0, got " + allowedLateness);
        }
        if (latePolicy == null) {
            throw new IllegalArgumentException("latePolicy must not be null");
        }
    }

    /** 合理的缺省配置：事件时间、窗口 1000ms、全部重叠匹配、迟到直接丢弃。 */
    public static EngineConfig eventTimeDefault(long windowMillis) {
        return new EngineConfig(EngineMode.EVENT_TIME, windowMillis,
                MatchPolicy.ALL_CANDIDATES, 0L, LatePolicy.DROP);
    }
}
