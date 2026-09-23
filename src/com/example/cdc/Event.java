package com.example.cdc;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 一条变更日志事件。
 *
 * <p>三种事件类型共享一条全局位置序列 {@code position}（从 1 开始、逐 1 递增）：
 * <ul>
 *   <li>DATA：某事务 txn 对表 table 的一行变更，op ∈ INSERT/UPDATE/DELETE；</li>
 *   <li>COMMIT：提交事务 txn，此前该事务的 DATA 在提交前对外不可见；</li>
 *   <li>ROLLBACK：回滚事务 txn，其缓存的 DATA 全部丢弃。</li>
 * </ul>
 *
 * <p>主键是 JSON 数组（支持复合主键），{@code oldPk} 用于 UPDATE 改主键的场景；
 * {@code oldValues}/{@code newValues} 分别为变更前后的列值（不含主键列也可）。
 * rawJson 保存规范化（紧凑、保持字段顺序）后的原文，用于 WAL 持久化与去重比对。
 */
public final class Event {

    public static final String DATA = "DATA";
    public static final String COMMIT = "COMMIT";
    public static final String ROLLBACK = "ROLLBACK";

    public static final String INSERT = "INSERT";
    public static final String UPDATE = "UPDATE";
    public static final String DELETE = "DELETE";

    public final long position;
    public final String type;
    public final String txn;
    public final String table;
    public final String op;
    /** 变更后的主键（DELETE 表示被删行的主键）。 */
    public final List<Object> pk;
    /** UPDATE 改主键前的旧主键；其余场景为 null。 */
    public final List<Object> oldPk;
    public final Map<String, Object> oldValues;
    public final Map<String, Object> newValues;
    public final String rawJson;

    private Event(long position, String type, String txn, String table, String op,
                  List<Object> pk, List<Object> oldPk,
                  Map<String, Object> oldValues, Map<String, Object> newValues,
                  String rawJson) {
        this.position = position;
        this.type = type;
        this.txn = txn;
        this.table = table;
        this.op = op;
        this.pk = pk;
        this.oldPk = oldPk;
        this.oldValues = oldValues;
        this.newValues = newValues;
        this.rawJson = rawJson;
    }

    @SuppressWarnings("unchecked")
    public static Event fromMap(Map<String, Object> m) {
        long pos = requirePositiveInt(m.get("position"), "position");
        String type = requireString(m.get("type"), "type");
        String txn = requireString(m.get("txn"), "txn");
        if (DATA.equals(type)) {
            type = DATA;
        } else if (COMMIT.equals(type)) {
            type = COMMIT;
        } else if (ROLLBACK.equals(type) || "ABORT".equals(type)) {
            type = ROLLBACK;
        } else {
            throw new IllegalArgumentException("未知 type: " + type + "（应为 DATA/COMMIT/ROLLBACK）");
        }

        String table = null;
        String op = null;
        List<Object> pk = null;
        List<Object> oldPk = null;
        Map<String, Object> oldValues = null;
        Map<String, Object> newValues = null;

        if (type.equals(DATA)) {
            table = requireString(m.get("table"), "table");
            op = requireString(m.get("op"), "op");
            if (!INSERT.equals(op) && !UPDATE.equals(op) && !DELETE.equals(op)) {
                throw new IllegalArgumentException("未知 op: " + op + "（应为 INSERT/UPDATE/DELETE）");
            }
            pk = requirePk(m.get("pk"), "pk");
            Object opk = m.get("oldPk");
            if (opk != null) {
                oldPk = requirePk(opk, "oldPk");
            }
            Object ov = m.get("oldValues");
            if (ov != null) {
                if (!(ov instanceof Map)) {
                    throw new IllegalArgumentException("oldValues 必须是 JSON 对象");
                }
                oldValues = new LinkedHashMap<>((Map<String, Object>) ov);
            }
            Object nv = m.get("newValues");
            if (nv != null) {
                if (!(nv instanceof Map)) {
                    throw new IllegalArgumentException("newValues 必须是 JSON 对象");
                }
                newValues = new LinkedHashMap<>((Map<String, Object>) nv);
            }
        }

        String raw = Json.write(m);
        return new Event(pos, type, txn, table, op, pk, oldPk, oldValues, newValues, raw);
    }

    /** 从已持久化的 JSON 行重建事件（WAL 重放使用）。 */
    public static Event fromRaw(String raw) {
        return fromMap(Json.parseObject(raw));
    }

    private static long requirePositiveInt(Object v, String field) {
        if (!(v instanceof Long)) {
            throw new IllegalArgumentException(field + " 必须是正整数");
        }
        long n = (Long) v;
        if (n <= 0) {
            throw new IllegalArgumentException(field + " 必须是正整数，实际为 " + n);
        }
        return n;
    }

    private static String requireString(Object v, String field) {
        if (!(v instanceof String) || ((String) v).isEmpty()) {
            throw new IllegalArgumentException(field + " 必须是非空字符串");
        }
        return (String) v;
    }

    private static List<Object> requirePk(Object v, String field) {
        if (!(v instanceof List) || ((List<?>) v).isEmpty()) {
            throw new IllegalArgumentException(field + " 必须是非空 JSON 数组");
        }
        return new ArrayList<>((List<?>) v);
    }

    /** 主键的规范化字符串（JSON 紧凑形式），作为 Map 键使用。 */
    public static String keyOf(List<Object> pk) {
        return Json.write(pk);
    }
}
