package com.example.bitemporal.json;

import com.example.bitemporal.data.SeedData;
import com.example.bitemporal.engine.BitemporalException;
import com.example.bitemporal.engine.BitemporalStore;
import com.example.bitemporal.engine.CommitLogEntry;
import com.example.bitemporal.model.BitemporalRecord;
import com.example.bitemporal.model.ChangeRequest;
import com.example.bitemporal.model.WriteMode;
import com.fasterxml.jackson.databind.JsonNode;

import java.time.LocalDate;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * JSON 协议分发。所有方法入参/出参均为可直接序列化的 Map/List/标量。
 *
 * <p>支持的操作（顶层 {@code op} 字段）：
 * <ul>
 *   <li>{@code info}：TZDB 版本、Java 版本、固定种子说明；</li>
 *   <li>{@code asOf}：双维点查询 {@code entityId/businessDate/observationDate}；</li>
 *   <li>{@code asOfAll}：全部实体在（业务日期，观察时刻）的点查询；</li>
 *   <li>{@code history}：某观察时刻看到的某实体有效时间历史；</li>
 *   <li>{@code commit}：一个事务提交一或多笔变更；</li>
 *   <li>{@code snapshot}：导出全部物理行（含已关闭的历史行）与提交日志；</li>
 *   <li>{@code scenario}：在同一装载数据的存储上按序执行多个 commit/asOf 步骤，
 *       用于复现“修订前观察 → 追溯修订 → 修订后观察”的完整手算链路。</li>
 * </ul>
 */
public class RequestService {

    public Map<String, Object> handle(JsonNode root) {
        if (root == null || !root.hasNonNull("op")) {
            throw new BitemporalException("request requires an \"op\" field");
        }
        String op = root.get("op").asText();
        BitemporalStore store = SeedData.createSeededStore();
        return switch (op) {
            case "info" -> info();
            case "asOf" -> asOf(store, root);
            case "asOfAll" -> asOfAll(store, root);
            case "history" -> history(store, root);
            case "commit" -> commit(store, root);
            case "snapshot" -> snapshot(store);
            case "scenario" -> scenario(store, root);
            default -> throw new BitemporalException("unknown op: " + op);
        };
    }

    private Map<String, Object> info() {
        Map<String, Object> m = base();
        m.put("timezone", TimeZoneInfo.current());
        m.put("seed", Map.of(
                "seedTransactionDate", SeedData.SEED_TX_DATE.toString(),
                "description", "fixed local fixture: team membership department/role history",
                "entities", List.of("E001", "E002", "E003")));
        m.put("semantics", Map.of(
                "intervalConvention", "half-open [from, to); null to means +infinity",
                "validTime", "business effective time",
                "recordedTime", "system/transaction time; revisions close old rows and append new ones",
                "writeModes", List.of("INSERT (reject overlap)", "CORRECTION (retroactive restatement)")));
        return m;
    }

    private Map<String, Object> asOf(BitemporalStore store, JsonNode n) {
        String entityId = text(n, "entityId");
        LocalDate business = date(n, "businessDate");
        LocalDate observation = date(n, "observationDate");
        Map<String, Object> m = base();
        m.put("query", Map.of(
                "entityId", entityId,
                "businessDate", business.toString(),
                "observationDate", observation.toString()));
        store.asOf(entityId, business, observation)
                .ifPresentOrElse(
                        r -> m.put("record", RecordJson.toMap(r)),
                        () -> m.put("record", null));
        return m;
    }

    private Map<String, Object> asOfAll(BitemporalStore store, JsonNode n) {
        LocalDate business = date(n, "businessDate");
        LocalDate observation = date(n, "observationDate");
        Map<String, Object> m = base();
        m.put("query", Map.of(
                "businessDate", business.toString(),
                "observationDate", observation.toString()));
        m.put("records", store.asOfAll(business, observation).stream()
                .map(RecordJson::toMap).toList());
        return m;
    }

    private Map<String, Object> history(BitemporalStore store, JsonNode n) {
        String entityId = text(n, "entityId");
        LocalDate observation = date(n, "observationDate");
        Map<String, Object> m = base();
        m.put("query", Map.of(
                "entityId", entityId,
                "observationDate", observation.toString()));
        m.put("history", store.history(entityId, observation).stream()
                .map(RecordJson::toMap).toList());
        return m;
    }

    private Map<String, Object> commit(BitemporalStore store, JsonNode n) {
        LocalDate txDate = date(n, "transactionDate");
        List<ChangeRequest> changes = parseChanges(n.get("changes"));
        var result = store.commit(txDate, changes);

        Map<String, Object> m = base();
        m.put("transactionDate", txDate.toString());
        m.put("committedChanges", changes.size());
        m.put("writtenRows", result.writtenRows().stream().map(RecordJson::toMap).toList());
        m.put("physicalRowCount", store.snapshot().size());
        return m;
    }

    private Map<String, Object> snapshot(BitemporalStore store) {
        Map<String, Object> m = base();
        m.put("rows", store.snapshot().stream().map(RecordJson::toMap).toList());
        m.put("commitLog", store.commitLog().stream().map(this::commitLogMap).toList());
        return m;
    }

    /**
     * scenario：{"steps":[ {"type":"commit",...}, {"type":"asOf",...}, ... ]}
     * 步骤按数组顺序执行，共享同一存储；任一步骤抛错则整体报错（不落任何部分结果语义）。
     */
    private Map<String, Object> scenario(BitemporalStore store, JsonNode n) {
        JsonNode steps = n.get("steps");
        if (steps == null || !steps.isArray() || steps.isEmpty()) {
            throw new BitemporalException("scenario requires a non-empty \"steps\" array");
        }
        List<Map<String, Object>> stepResults = new ArrayList<>();
        for (JsonNode step : steps) {
            String type = text(step, "type");
            Map<String, Object> stepResult = new LinkedHashMap<>();
            stepResult.put("type", type);
            switch (type) {
                case "commit" -> {
                    LocalDate txDate = date(step, "transactionDate");
                    List<ChangeRequest> changes = parseChanges(step.get("changes"));
                    var result = store.commit(txDate, changes);
                    stepResult.put("transactionDate", txDate.toString());
                    stepResult.put("committedChanges", changes.size());
                    stepResult.put("writtenRowIds",
                            result.writtenRows().stream().map(BitemporalRecord::rowId).toList());
                }
                case "asOf" -> {
                    String entityId = text(step, "entityId");
                    LocalDate business = date(step, "businessDate");
                    LocalDate observation = date(step, "observationDate");
                    stepResult.put("query", Map.of(
                            "entityId", entityId,
                            "businessDate", business.toString(),
                            "observationDate", observation.toString()));
                    stepResult.put("record",
                            store.asOf(entityId, business, observation)
                                    .map(RecordJson::toMap).orElse(null));
                }
                case "history" -> {
                    String entityId = text(step, "entityId");
                    LocalDate observation = date(step, "observationDate");
                    stepResult.put("history",
                            store.history(entityId, observation).stream()
                                    .map(RecordJson::toMap).toList());
                }
                default -> throw new BitemporalException("unknown scenario step type: " + type);
            }
            stepResults.add(stepResult);
        }
        Map<String, Object> m = base();
        m.put("steps", stepResults);
        m.put("finalPhysicalRowCount", store.snapshot().size());
        return m;
    }

    // ---- 解析辅助 ---------------------------------------------------------

    private List<ChangeRequest> parseChanges(JsonNode arr) {
        if (arr == null || !arr.isArray() || arr.isEmpty()) {
            throw new BitemporalException("commit requires a non-empty \"changes\" array");
        }
        List<ChangeRequest> out = new ArrayList<>();
        for (JsonNode c : arr) {
            String modeText = text(c, "mode").toUpperCase(java.util.Locale.ROOT);
            WriteMode mode;
            try {
                mode = WriteMode.valueOf(modeText);
            } catch (IllegalArgumentException e) {
                throw new BitemporalException("mode must be INSERT or CORRECTION, got: " + modeText);
            }
            LocalDate validFrom = date(c, "validFrom");
            LocalDate validTo = c.hasNonNull("validTo")
                    ? LocalDate.parse(c.get("validTo").asText()) : null;
            out.add(new ChangeRequest(
                    text(c, "entityId"),
                    text(c, "department"),
                    text(c, "role"),
                    validFrom, validTo, mode));
        }
        return out;
    }

    private Map<String, Object> commitLogMap(CommitLogEntry e) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("transactionDate", e.transactionDate().toString());
        m.put("changeCount", e.changeCount());
        m.put("changes", e.changes());
        return m;
    }

    private static Map<String, Object> base() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("ok", true);
        return m;
    }

    private static String text(JsonNode n, String field) {
        if (n == null || !n.hasNonNull(field)) {
            throw new BitemporalException("missing required field: " + field);
        }
        return n.get(field).asText();
    }

    private static LocalDate date(JsonNode n, String field) {
        String raw = text(n, field);
        try {
            return LocalDate.parse(raw);
        } catch (java.time.format.DateTimeParseException e) {
            throw new BitemporalException("field " + field + " must be ISO date yyyy-MM-dd: " + raw);
        }
    }
}
