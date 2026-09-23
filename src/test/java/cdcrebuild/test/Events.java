package cdcrebuild.test;

import cdcrebuild.model.Event;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;

/** 测试用事件构造器，支持 varargs 式行数据。 */
public final class Events {

    private Events() {
    }

    public static Map<String, Object> row(Object... kv) {
        if (kv.length % 2 != 0) {
            throw new IllegalArgumentException("行数据必须是键值对");
        }
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    public static Event begin(long pos, String txId) {
        return Event.fromMap(base(pos, txId, "BEGIN"));
    }

    public static Event commit(long pos, String txId) {
        return Event.fromMap(base(pos, txId, "COMMIT"));
    }

    public static Event rollback(long pos, String txId) {
        return Event.fromMap(base(pos, txId, "ROLLBACK"));
    }

    public static Event insert(long pos, String txId, String table, Map<String, Object> newRow) {
        Map<String, Object> m = base(pos, txId, "DATA");
        m.put("table", table);
        m.put("op", "INSERT");
        m.put("new", newRow);
        return Event.fromMap(m);
    }

    public static Event update(long pos, String txId, String table,
                               Map<String, Object> oldRow, Map<String, Object> newRow) {
        Map<String, Object> m = base(pos, txId, "DATA");
        m.put("table", table);
        m.put("op", "UPDATE");
        m.put("old", oldRow);
        m.put("new", newRow);
        m.put("pkChanged", !String.valueOf(oldRow.get("id")).equals(String.valueOf(newRow.get("id"))));
        return Event.fromMap(m);
    }

    public static Event delete(long pos, String txId, String table, Map<String, Object> oldRow) {
        Map<String, Object> m = base(pos, txId, "DATA");
        m.put("table", table);
        m.put("op", "DELETE");
        m.put("old", oldRow);
        return Event.fromMap(m);
    }

    private static Map<String, Object> base(long pos, String txId, String type) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("pos", pos);
        m.put("txId", txId);
        m.put("type", type);
        return m;
    }

    /** 每个测试使用独立的临时数据目录。 */
    public static Path tempDir(String prefix) {
        try {
            return Files.createTempDirectory(prefix);
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
    }
}
