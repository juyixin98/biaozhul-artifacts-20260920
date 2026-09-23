package com.example.cdc;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 测试用事件构造器。 */
final class Ev {

    static Map<String, Object> data(long pos, String txn, String table, String op,
                                    List<Object> pk, List<Object> oldPk,
                                    Map<String, Object> n) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("position", pos);
        m.put("type", "DATA");
        m.put("txn", txn);
        m.put("table", table);
        m.put("op", op);
        m.put("pk", pk);
        if (oldPk != null) {
            m.put("oldPk", oldPk);
        }
        m.put("newValues", n);
        return m;
    }

    static Map<String, Object> insert(long pos, String txn, String table,
                                      List<Object> pk, Map<String, Object> values) {
        return data(pos, txn, table, "INSERT", pk, null, values);
    }

    static Map<String, Object> update(long pos, String txn, String table,
                                      List<Object> pk, Map<String, Object> values) {
        return data(pos, txn, table, "UPDATE", pk, null, values);
    }

    static Map<String, Object> pkChange(long pos, String txn, String table,
                                        List<Object> oldPk, List<Object> newPk,
                                        Map<String, Object> values) {
        return data(pos, txn, table, "UPDATE", newPk, oldPk, values);
    }

    static Map<String, Object> delete(long pos, String txn, String table, List<Object> pk) {
        return data(pos, txn, table, "DELETE", pk, null, null);
    }

    static Map<String, Object> commit(long pos, String txn) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("position", pos);
        m.put("type", "COMMIT");
        m.put("txn", txn);
        return m;
    }

    static Map<String, Object> rollback(long pos, String txn) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("position", pos);
        m.put("type", "ROLLBACK");
        m.put("txn", txn);
        return m;
    }

    static Map<String, Object> v(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    private Ev() {
    }
}
