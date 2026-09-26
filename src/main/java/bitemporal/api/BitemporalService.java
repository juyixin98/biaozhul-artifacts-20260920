package bitemporal.api;

import bitemporal.error.OverlapRejectedException;
import bitemporal.error.RecordNotFoundException;
import bitemporal.error.ValidationException;
import bitemporal.model.CommitResult;
import bitemporal.model.QueryRequest;
import bitemporal.model.TemporalRecord;
import bitemporal.model.TransactionRequest;
import bitemporal.store.BitemporalStore;
import bitemporal.store.SeedData;
import bitemporal.time.TzdbInfo;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 无状态服务层：把命令 DTO 分发到 {@link BitemporalStore}，统一异常→错误码映射，
 * 并把时区数据库版本等运行信息附加到查询结果。
 */
public class BitemporalService {

    private final BitemporalStore store;

    public BitemporalService(BitemporalStore store) {
        this.store = store;
    }

    /** 载入本地固定种子数据；要求库为空（避免重复载入造成重叠）。 */
    public CommitResult seed() {
        if (!store.recordIds().isEmpty()) {
            throw new ValidationException("store is not empty; run reset before seed");
        }
        return store.commit(SeedData.seedTransaction());
    }

    public CommitResult commit(TransactionRequest request) {
        return store.commit(request);
    }

    public Map<String, Object> query(QueryRequest request) {
        QueryRequest.Resolved at = request.resolve();
        List<TemporalRecord> rows = store.asOf(request);
        return queryEnvelope(rows, at.validAt(), at.systemAt());
    }

    public Map<String, Object> history(String recordId) {
        if (recordId == null || recordId.isBlank()) {
            throw new ValidationException("history requires recordId");
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("recordId", recordId);
        out.put("rows", store.history(recordId));
        return out;
    }

    public void reset() {
        store.reset();
    }

    public Map<String, Object> info() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("tzdbVersion", TzdbInfo.version().orElse("unknown"));
        out.put("defaultZone", TzdbInfo.defaultZoneId());
        out.put("availableZoneCount", TzdbInfo.availableZoneCount());
        out.put("tzdbSummary", TzdbInfo.summary());
        out.put("lastCommitAt", store.lastCommitAt().orElse(null));
        out.put("recordIds", store.recordIds());
        return out;
    }

    /**
     * 顺序执行批处理步骤。单步失败不抛出：记入 {@link BatchResult.StepOutcome}，
     * 除非该步声明 continueOnError，否则中止后续步骤。
     */
    public BatchResult batch(List<BatchStep> steps) {
        if (steps == null || steps.isEmpty()) {
            throw new ValidationException("batch requires at least one step");
        }
        List<BatchResult.StepOutcome> outcomes = new ArrayList<>();
        Integer abortedAt = null;
        for (int i = 0; i < steps.size(); i++) {
            BatchStep step = steps.get(i);
            try {
                Object result = executeStep(step);
                outcomes.add(BatchResult.StepOutcome.ok(i, step.type(), result));
            } catch (RuntimeException e) {
                ApiResponse.ErrorBody error = mapError(e);
                outcomes.add(BatchResult.StepOutcome.failed(i, step.type(), error));
                if (!step.shouldContinueOnError()) {
                    abortedAt = i;
                    break;
                }
            }
        }
        return new BatchResult(List.copyOf(outcomes), abortedAt);
    }

    private Object executeStep(BatchStep step) {
        String type = step.type() == null ? "" : step.type().trim().toLowerCase(java.util.Locale.ROOT);
        return switch (type) {
            case "seed" -> seed();
            case "reset" -> {
                reset();
                yield Map.of("reset", true);
            }
            case "commit" -> commit(new TransactionRequest(
                    step.txnId(), step.committedAt(), step.changes()));
            case "query" -> query(new QueryRequest(
                    step.observationTime(), step.validAt(), step.systemAt(), step.recordId()));
            case "history" -> history(step.recordId());
            case "info" -> info();
            default -> throw new ValidationException(
                    "unsupported batch step type '" + step.type() + "'");
        };
    }

    /** 把领域异常映射成稳定错误码，供 CLI 决定退出码。 */
    public static ApiResponse.ErrorBody mapError(RuntimeException e) {
        if (e instanceof OverlapRejectedException overlap) {
            return new ApiResponse.ErrorBody(
                    "OVERLAP_REJECTED", e.getMessage(), overlap.recordId());
        }
        if (e instanceof RecordNotFoundException) {
            return new ApiResponse.ErrorBody("RECORD_NOT_FOUND", e.getMessage(), null);
        }
        if (e instanceof ValidationException) {
            return new ApiResponse.ErrorBody("VALIDATION_ERROR", e.getMessage(), null);
        }
        return new ApiResponse.ErrorBody("INTERNAL_ERROR", String.valueOf(e.getMessage()), null);
    }

    private Map<String, Object> queryEnvelope(List<TemporalRecord> rows,
                                              Instant validAt,
                                              Instant systemAt) {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("observedAt", Map.of("validAt", validAt, "systemAt", systemAt));
        out.put("count", rows.size());
        out.put("rows", rows);
        return out;
    }
}
