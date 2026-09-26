package bitemporal.model;

import java.time.Instant;

/**
 * as-of 双维查询请求。
 *
 * <p>两种写法等价：给 {@code observationTime} 同时作为两个维度的观察时刻；
 * 或分别用 {@code validAt} / {@code systemAt} 指定（优先于 observationTime）。</p>
 *
 * @param observationTime 业务与系统两个维度共用的观察时刻（可选）
 * @param validAt         业务有效时间观察时刻（可选）
 * @param systemAt        系统记录时间观察时刻（可选）
 * @param recordId        仅查指定记录；省略则返回该时刻全部记录
 */
public record QueryRequest(
        Instant observationTime,
        Instant validAt,
        Instant systemAt,
        String recordId) {

    /** 解析后的双维观察时刻。 */
    public record Resolved(Instant validAt, Instant systemAt) {
    }

    public Resolved resolve() {
        Instant valid = validAt != null ? validAt : observationTime;
        Instant system = systemAt != null ? systemAt : observationTime;
        if (valid == null || system == null) {
            throw new bitemporal.error.ValidationException(
                    "query requires observationTime, or both validAt and systemAt");
        }
        return new Resolved(valid, system);
    }
}
