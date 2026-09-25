package drvb.http;

import drvb.json.Json;
import drvb.model.Event;
import drvb.model.ProcessResult;
import drvb.version.Binding;
import drvb.version.ReclamationService;
import drvb.version.RuleVersion;
import drvb.version.RuleVersionTable;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** HTTP 层 JSON 映射：请求体 -&gt; 领域对象，领域对象 -&gt; 响应 JSON。 */
final class Dtos {

    private Dtos() {
    }

    // ---------------------------------------------------------------
    // 请求解析
    // ---------------------------------------------------------------

    static Event toEvent(Object body) {
        Map<String, Object> m = Json.asObject(body);
        String eventId = Json.getString(m, "eventId");
        if (!m.containsKey("eventTime")) {
            throw new IllegalArgumentException("缺少必填字段 'eventTime'（事件时间，毫秒）");
        }
        long eventTime = Json.asLong(m.get("eventTime"));
        String type = m.get("type") == null ? null : Json.asString(m.get("type"));
        Object payload = m.get("payload");
        if (payload != null && !(payload instanceof Map)) {
            throw new IllegalArgumentException("'payload' 必须是 JSON 对象");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> p = (Map<String, Object>) payload;
        return new Event(eventId, eventTime, type, p);
    }

    // ---------------------------------------------------------------
    // 响应序列化
    // ---------------------------------------------------------------

    static Map<String, Object> resultToJson(ProcessResult r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("eventId", r.eventId());
        m.put("eventTime", r.eventTime());
        m.put("processingTime", r.processingTime());
        m.put("watermarkBefore", wm(r.watermarkBefore()));
        m.put("watermarkAfter", wm(r.watermarkAfter()));
        m.put("late", r.late());
        if (r.rejected()) {
            m.put("status", "REJECTED");
            m.put("rejectReason", r.rejectReason().name());
            m.put("detail", r.detail());
        } else {
            m.put("status", Boolean.TRUE.equals(r.matched()) ? "MATCHED" : "FILTERED_OUT");
            m.put("matched", r.matched());
            m.put("ruleVersionId", r.ruleVersionId());
            m.put("bindingSeq", r.bindingSeq());
        }
        return m;
    }

    private static Object wm(long v) {
        return v == Long.MIN_VALUE ? null : v;
    }

    static Map<String, Object> bindingToJson(Binding b) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("seq", b.seq());
        m.put("effectiveFrom", b.effectiveFrom());
        m.put("ruleVersionId", b.ruleVersionId());
        m.put("publishedAt", b.publishedAt());
        m.put("operation", b.operation());
        m.put("note", b.note());
        return m;
    }

    static Map<String, Object> versionToJson(RuleVersion v) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", v.id());
        m.put("name", v.name());
        m.put("createdAt", v.createdAt());
        m.put("predicate", v.predicateSpec());
        return m;
    }

    static Map<String, Object> stateToJson(RuleVersionTable.Snapshot snap,
                                           long watermark,
                                           long allowedLateness,
                                           long retentionHorizon,
                                           long gate,
                                           long processingTime,
                                           Map<String, ReclamationService.Eligibility> eligibility,
                                           List<ProcessResult> recentResults) {
        Map<String, Object> root = new LinkedHashMap<>();
        root.put("processingTime", processingTime);
        root.put("watermark", watermark == Long.MIN_VALUE ? null : watermark);
        root.put("allowedLateness", allowedLateness);
        root.put("retentionHorizon", retentionHorizon);
        root.put("reclaimGate", gate == Long.MIN_VALUE ? null : gate);

        List<Object> versions = new ArrayList<>();
        for (RuleVersion v : snap.aliveVersions().values()) {
            Map<String, Object> vj = versionToJson(v);
            ReclamationService.Eligibility e = eligibility.get(v.id());
            vj.put("reclaimEligible", e != null && e.eligible());
            if (e != null) {
                vj.put("reclaimDetail", e.reason());
            }
            versions.add(vj);
        }
        root.put("versions", versions);

        List<Object> tombs = new ArrayList<>();
        for (String id : snap.tombstones()) {
            tombs.add(Json.obj("id", id, "reclaimedAt", snap.reclaimedAt().get(id)));
        }
        root.put("reclaimedVersions", tombs);

        List<Object> bindings = new ArrayList<>();
        List<Binding> bs = snap.bindings();
        for (int i = 0; i < bs.size(); i++) {
            Map<String, Object> bj = bindingToJson(bs.get(i));
            bj.put("effectiveTo",
                    i + 1 < bs.size() ? bs.get(i + 1).effectiveFrom() : null);
            bindings.add(bj);
        }
        root.put("bindings", bindings);

        List<Object> rs = new ArrayList<>();
        for (ProcessResult r : recentResults) {
            rs.add(resultToJson(r));
        }
        root.put("recentResults", rs);
        return root;
    }
}
