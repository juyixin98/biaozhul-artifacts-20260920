package bitemporal.model;

import java.time.Instant;
import java.util.List;

/**
 * 一次事务请求：同一事务内的多个变更原子提交（全部成功或全部不生效）。
 *
 * @param txnId       可选事务标识；缺省自动生成
 * @param committedAt 可选系统记录时间（便于复算）；缺省取系统时钟当前时刻，
 *                    但必须严格晚于本库上次提交时间
 * @param changes     有序变更列表，按序应用，后一个变更能看到同事务前一个变更的结果
 */
public record TransactionRequest(
        String txnId,
        Instant committedAt,
        List<ChangeRequest> changes) {
}
