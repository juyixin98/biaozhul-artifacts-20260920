package bitemporal.api;

import bitemporal.model.ChangeRequest;

import java.time.Instant;
import java.util.List;

/**
 * 批处理中的单个步骤。按 {@code type} 分发：
 * seed / commit / query / history / reset / info。
 *
 * @param continueOnError 仅用于 batch：该步骤业务失败（如重叠拒绝）后是否继续后续步骤；
 *                        缺省 false（失败即中止后续步骤，已提交的前序步骤不回滚）
 */
public record BatchStep(
        String type,
        String txnId,
        Instant committedAt,
        List<ChangeRequest> changes,
        Instant observationTime,
        Instant validAt,
        Instant systemAt,
        String recordId,
        Boolean continueOnError) {

    public boolean shouldContinueOnError() {
        return Boolean.TRUE.equals(continueOnError);
    }
}
