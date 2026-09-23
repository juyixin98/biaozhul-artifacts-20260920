package cdcrebuild.test;

import cdcrebuild.codec.Json;
import cdcrebuild.model.Event;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * 源事务解释器（参考模型 / oracle）：直接按事件流的事务边界顺序执行，
 * 没有缺口、持久化等工程因素。随机化测试用它的最终表与引擎对账——
 * 两者必须逐键逐字段一致。
 */
public final class ReferenceInterpreter {

    private final String pkColumn;
    private final TreeMap<String, TreeMap<String, Map<String, Object>>> tables = new TreeMap<>();
    private final Map<String, List<Event>> open = new LinkedHashMap<>();

    public ReferenceInterpreter(String pkColumn) {
        this.pkColumn = pkColumn;
    }

    public void apply(Event e) {
        switch (e.type) {
            case BEGIN:
                open.put(e.txId, new ArrayList<>());
                break;
            case DATA:
                open.get(e.txId).add(e);
                break;
            case COMMIT: {
                List<Event> staged = open.remove(e.txId);
                for (Event d : staged) {
                    applyData(d);
                }
                break;
            }
            case ROLLBACK:
                open.remove(e.txId);
                break;
            default:
                throw new IllegalStateException("未知类型");
        }
    }

    private void applyData(Event e) {
        TreeMap<String, Map<String, Object>> rows =
                tables.computeIfAbsent(e.table, t -> new TreeMap<>());
        switch (e.op) {
            case INSERT: {
                String key = Json.canonical(e.newRow.get(pkColumn));
                if (rows.containsKey(key)) {
                    throw new IllegalStateException("参考模型: 主键冲突 " + key);
                }
                rows.put(key, new LinkedHashMap<>(e.newRow));
                break;
            }
            case DELETE:
                rows.remove(Json.canonical(e.oldRow.get(pkColumn)));
                break;
            case UPDATE: {
                String oldKey = Json.canonical(e.oldRow.get(pkColumn));
                String newKey = Json.canonical(e.newRow.get(pkColumn));
                if (!oldKey.equals(newKey)) {
                    rows.remove(oldKey);
                }
                rows.put(newKey, new LinkedHashMap<>(e.newRow));
                break;
            }
            default:
                throw new IllegalStateException();
        }
    }

    /** 不可变结构快照（深拷贝）。 */
    public Map<String, Object> snapshot() {
        Map<String, Object> out = new LinkedHashMap<>();
        for (var en : tables.entrySet()) {
            List<Object> rows = new ArrayList<>();
            for (Map<String, Object> row : en.getValue().values()) {
                rows.add(new LinkedHashMap<>(row));
            }
            out.put(en.getKey(), rows);
        }
        return out;
    }

    public boolean openTxnsEmpty() {
        return open.isEmpty();
    }
}
