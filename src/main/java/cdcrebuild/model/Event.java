package cdcrebuild.model;

import cdcrebuild.codec.Json;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 变更日志事件（不可变）。
 *
 * 事件按 pos（源位置，从 1 开始的严格递增 long）投递：
 *   BEGIN  {pos, txId, type:"BEGIN"}
 *   DATA   {pos, txId, type:"DATA", table, op, old, new, pkChanged}
 *   COMMIT {pos, txId, type:"COMMIT"}
 *   ROLLBACK {pos, txId, type:"ROLLBACK"}
 *
 * DATA 事件携带整行旧值/新值，便于在“重放全部历史”时与源事务解释器逐行对账。
 * 主键列默认名为 id（可通过构造参数调整）；主键变更由 pkChanged 显式标记，
 * 此时 old 与 new 的主键值不同，引擎先删旧键再插新键。
 */
public final class Event {

    public enum Type {
        BEGIN, DATA, COMMIT, ROLLBACK
    }

    public enum Op {
        INSERT, UPDATE, DELETE
    }

    public final long pos;
    public final String txId;
    public final Type type;

    // DATA 事件专有
    public final String table;
    public final Op op;
    public final Map<String, Object> oldRow;
    public final Map<String, Object> newRow;
    public final boolean pkChanged;

    private final String canonical;

    private Event(long pos, String txId, Type type, String table, Op op,
                  Map<String, Object> oldRow, Map<String, Object> newRow, boolean pkChanged) {
        this.pos = pos;
        this.txId = txId;
        this.type = type;
        this.table = table;
        this.op = op;
        this.oldRow = oldRow;
        this.newRow = newRow;
        this.pkChanged = pkChanged;
        this.canonical = Json.canonical(toMap());
    }

    /** 从 HTTP 请求体解析并做完整的结构 / 语义校验。非法时抛 ValidationException。 */
    @SuppressWarnings("unchecked")
    public static Event fromMap(Map<String, Object> m) {
        if (m == null) {
            throw new ValidationException("请求体必须是 JSON 对象");
        }
        long pos;
        Object posVal = m.get("pos");
        if (posVal instanceof Number) {
            double d = ((Number) posVal).doubleValue();
            if (d <= 0 || d != Math.rint(d) || d > Long.MAX_VALUE) {
                throw new ValidationException("pos 必须是正整数");
            }
            pos = (long) d;
        } else {
            throw new ValidationException("缺少或非法的 pos（正整数）");
        }

        String txId = requireString(m, "txId");
        if (txId.isEmpty()) {
            throw new ValidationException("txId 不能为空字符串");
        }
        String typeStr = requireString(m, "type");
        Type type;
        try {
            type = Type.valueOf(typeStr);
        } catch (IllegalArgumentException e) {
            throw new ValidationException("非法 type: " + typeStr + "（允许 BEGIN/DATA/COMMIT/ROLLBACK）");
        }

        String table = null;
        Op op = null;
        Map<String, Object> oldRow = null;
        Map<String, Object> newRow = null;
        boolean pkChanged = false;

        if (type == Type.DATA) {
            table = requireString(m, "table");
            if (table.isEmpty()) {
                throw new ValidationException("table 不能为空字符串");
            }
            String opStr = requireString(m, "op");
            try {
                op = Op.valueOf(opStr);
            } catch (IllegalArgumentException e) {
                throw new ValidationException("非法 op: " + opStr + "（允许 INSERT/UPDATE/DELETE）");
            }
            oldRow = asRow(m.get("old"), "old");
            newRow = asRow(m.get("new"), "new");
            Object pkc = m.get("pkChanged");
            if (pkc != null) {
                if (!(pkc instanceof Boolean)) {
                    throw new ValidationException("pkChanged 必须是布尔值");
                }
                pkChanged = (Boolean) pkc;
            }
            switch (op) {
                case INSERT:
                    if (newRow == null) {
                        throw new ValidationException("INSERT 事件必须提供 new（新行）");
                    }
                    break;
                case UPDATE:
                    if (oldRow == null || newRow == null) {
                        throw new ValidationException("UPDATE 事件必须同时提供 old 与 new");
                    }
                    break;
                case DELETE:
                    if (oldRow == null) {
                        throw new ValidationException("DELETE 事件必须提供 old（被删行）");
                    }
                    break;
                default:
                    throw new ValidationException("未覆盖的 op 分支");
            }
        } else {
            for (String forbidden : new String[]{"table", "op", "old", "new", "pkChanged"}) {
                if (m.containsKey(forbidden)) {
                    throw new ValidationException(forbidden + " 只允许出现在 DATA 事件上");
                }
            }
        }
        return new Event(pos, txId, type, table, op, oldRow, newRow, pkChanged);
    }

    private static String requireString(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (!(v instanceof String)) {
            throw new ValidationException("缺少或非法的 " + key + "（字符串）");
        }
        return (String) v;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> asRow(Object v, String key) {
        if (v == null) {
            return null;
        }
        if (!(v instanceof Map)) {
            throw new ValidationException(key + " 必须是行对象（JSON object）");
        }
        return (Map<String, Object>) v;
    }

    public String canonical() {
        return canonical;
    }

    /** 转回 Map（LinkedHashMap 保持字段顺序，便于阅读日志与响应）。 */
    public Map<String, Object> toMap() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("pos", pos);
        m.put("txId", txId);
        m.put("type", type.name());
        if (type == Type.DATA) {
            m.put("table", table);
            m.put("op", op.name());
            if (oldRow != null) {
                m.put("old", oldRow);
            }
            if (newRow != null) {
                m.put("new", newRow);
            }
            m.put("pkChanged", pkChanged);
        }
        return m;
    }

    @Override
    public String toString() {
        return "Event#" + pos + "(" + type + ", tx=" + txId
                + (table != null ? ", " + table + "/" + op : "") + ")";
    }
}
