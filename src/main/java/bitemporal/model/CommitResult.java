package bitemporal.model;

import java.time.Instant;
import java.util.List;

/**
 * 单次提交成功的结果。
 *
 * @param txnId       事务标识
 * @param committedAt 实际系统记录时间
 * @param written     本次事务新写入的版本行（被封口的旧行不在此列）
 */
public record CommitResult(
        String txnId,
        Instant committedAt,
        List<TemporalRecord> written) {
}
