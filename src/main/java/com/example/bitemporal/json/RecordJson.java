package com.example.bitemporal.json;

import com.example.bitemporal.model.BitemporalRecord;

import java.util.LinkedHashMap;
import java.util.Map;

/** 物理行的 JSON 表示；validTo / recordedTo 为 null 即开放端正无穷。 */
public final class RecordJson {

    private RecordJson() {
    }

    public static Map<String, Object> toMap(BitemporalRecord r) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("rowId", r.rowId());
        m.put("entityId", r.entityId());
        m.put("department", r.department());
        m.put("role", r.role());
        m.put("validFrom", r.valid().from().toString());
        m.put("validTo", r.valid().to() == null ? null : r.valid().to().toString());
        m.put("recordedFrom", r.recorded().from().toString());
        m.put("recordedTo", r.recorded().to() == null ? null : r.recorded().to().toString());
        m.put("validInterval", r.valid().toString());
        m.put("recordedInterval", r.recorded().toString());
        return m;
    }
}
