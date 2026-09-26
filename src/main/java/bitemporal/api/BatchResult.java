package bitemporal.api;

import java.util.List;

/**
 * batch 执行结果：每步一个输出；某步业务失败时记录错误并按 continueOnError 决定是否继续。
 *
 * @param steps        各步骤输出（顺序执行）
 * @param abortedAt    首个未被 continueOnError 容忍的失败步骤下标；无中止为 null
 */
public record BatchResult(List<StepOutcome> steps, Integer abortedAt) {

    public record StepOutcome(
            int index,
            String type,
            boolean success,
            Object result,
            ApiResponse.ErrorBody error) {

        static StepOutcome ok(int index, String type, Object result) {
            return new StepOutcome(index, type, true, result, null);
        }

        static StepOutcome failed(int index, String type, ApiResponse.ErrorBody error) {
            return new StepOutcome(index, type, false, null, error);
        }
    }
}
