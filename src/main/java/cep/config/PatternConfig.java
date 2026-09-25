package cep.config;

import java.util.Objects;

/**
 * 模式 "A 之后出现 B，且两者之间没有 C" 的配置（全部可注入、可配置）。
 *
 * @param aKey            起始事件的 key
 * @param bKey            结束事件的 key
 * @param cKey            打断事件的 key
 * @param windowMs        时间窗口（&gt;0），B.timestamp - A.timestamp ≤ windowMs 才算匹配（边界包含）
 * @param policy          多 A 候选 / 重叠匹配策略
 * @param latePolicy      迟到事件策略
 * @param outOfOrderBound 乱序容忍度（≥0），自动水位 wm = maxObservedEventTime - outOfOrderBound
 * @param allowedLateness 允许迟到的宽限（≥0），ts + allowedLateness < wm 判定为迟到
 * @param emitTimeouts    是否为等不到 B 的 A 发出超时
 */
public record PatternConfig(String aKey, String bKey, String cKey, long windowMs,
                            MatchPolicy policy, LatePolicy latePolicy,
                            long outOfOrderBound, long allowedLateness,
                            boolean emitTimeouts) {

    public PatternConfig {
        Objects.requireNonNull(aKey, "aKey");
        Objects.requireNonNull(bKey, "bKey");
        Objects.requireNonNull(cKey, "cKey");
        Objects.requireNonNull(policy, "policy");
        Objects.requireNonNull(latePolicy, "latePolicy");
        if (aKey.equals(bKey) || aKey.equals(cKey) || bKey.equals(cKey)) {
            throw new IllegalArgumentException("aKey/bKey/cKey 必须互不相同: "
                    + aKey + "," + bKey + "," + cKey);
        }
        if (windowMs <= 0) {
            throw new IllegalArgumentException("windowMs 必须 > 0: " + windowMs);
        }
        if (outOfOrderBound < 0) {
            throw new IllegalArgumentException("outOfOrderBound 必须 >= 0: " + outOfOrderBound);
        }
        if (allowedLateness < 0) {
            throw new IllegalArgumentException("allowedLateness 必须 >= 0: " + allowedLateness);
        }
    }

    /** 顺序输入（bound=0）、默认 DROP、不发超时的最简配置。 */
    public static PatternConfig of(String aKey, String bKey, String cKey,
                                   long windowMs, MatchPolicy policy) {
        return new PatternConfig(aKey, bKey, cKey, windowMs, policy,
                LatePolicy.DROP, 0L, 0L, true);
    }

    public PatternConfig withPolicy(MatchPolicy p) {
        return new PatternConfig(aKey, bKey, cKey, windowMs, p, latePolicy,
                outOfOrderBound, allowedLateness, emitTimeouts);
    }

    public PatternConfig withLate(LatePolicy lp, long allowedLateness) {
        return new PatternConfig(aKey, bKey, cKey, windowMs, policy, lp,
                outOfOrderBound, allowedLateness, emitTimeouts);
    }

    public PatternConfig withOutOfOrderBound(long bound) {
        return new PatternConfig(aKey, bKey, cKey, windowMs, policy, latePolicy,
                bound, allowedLateness, emitTimeouts);
    }

    public PatternConfig withEmitTimeouts(boolean value) {
        return new PatternConfig(aKey, bKey, cKey, windowMs, policy, latePolicy,
                outOfOrderBound, allowedLateness, value);
    }
}
