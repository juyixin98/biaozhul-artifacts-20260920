package bitemporal.model;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonPropertyOrder;

import java.time.Instant;

/**
 * 双时态记录的一个不可变版本行。
 *
 * <p>每行同时携带两条时间轴：</p>
 * <ul>
 *   <li>业务有效时间 {@code [validFrom, validTo)}：事实在业务世界中成立的时间；</li>
 *   <li>系统记录时间 {@code [systemFrom, systemTo)}：该行在数据库中“被相信”的时间。</li>
 * </ul>
 * <p>修订不会更新或删除旧行，只把旧行的 {@code systemTo} 封口，再写入新行，
 * 因此任意历史观察时刻的查询结果都可以重现。</p>
 *
 * @param versionId  同一 recordId 内单调递增的版本行号
 * @param validTo    业务有效时间终点（排除），{@code null} 表示至今
 * @param systemTo   系统记录时间终点（排除），{@code null} 表示当前仍有效
 * @param txnId      写入该行的事务标识
 */
@JsonPropertyOrder({
        "recordId", "versionId", "data",
        "validFrom", "validTo",
        "systemFrom", "systemTo",
        "txnId"
})
@JsonInclude(JsonInclude.Include.NON_NULL)
public record TemporalRecord(
        String recordId,
        long versionId,
        String data,
        Instant validFrom,
        Instant validTo,
        Instant systemFrom,
        Instant systemTo,
        String txnId) {

    public Interval validInterval() {
        return new Interval(validFrom, validTo);
    }

    public Interval systemInterval() {
        return new Interval(systemFrom, systemTo);
    }

    /** 返回一份系统时间在 {@code closedAt} 封口的副本（不可变对象，原行不变）。 */
    public TemporalRecord withSystemTo(Instant closedAt) {
        return new TemporalRecord(recordId, versionId, data,
                validFrom, validTo, systemFrom, closedAt, txnId);
    }
}
