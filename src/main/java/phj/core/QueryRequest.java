package phj.core;

import phj.json.Json;

import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 一次连接查询请求：
 * <pre>
 * {
 *   "joinType": "INNER" | "LEFT",
 *   "keys": ["k"] 或 [["lk","rk"], ...],
 *   "left":  { "name":..., "columns":[...], "rows":[[...]] },
 *   "right": { ... },
 *   "options": {
 *     "memoryThresholdRows": 4,        // 建表侧单分区可驻留内存的行数阈值
 *     "partitions": 4,                 // 分区数；0/AUTO 表示按阈值估算
 *     "diskQuotaBytes": 10000,         // -1（默认）不限制；0 完全禁止落盘
 *     "spillDir": "/tmp/phj",          // 溢写根目录，默认 java.io.tmpdir
 *     "keepSpillFiles": false,         // 保留分区文件以便检查 / 导出
 *     "exportPlan": true               // 响应中附带执行计划与统计
 *   }
 * }
 * </pre>
 */
public final class QueryRequest {

    public JoinType joinType = JoinType.INNER;
    public List<JoinKeyPair> keyPairs = new ArrayList<>();
    public Relation left;
    public Relation right;

    public long memoryThresholdRows = 4;
    public int partitions = 0; // 0 = 自动
    public long diskQuotaBytes = -1;
    public Path spillDir;
    public boolean keepSpillFiles = false;
    public boolean exportPlan = true;
    public int maxRecursiveLevels = 16;

    public static QueryRequest fromJson(Map<String, Object> req) {
        QueryRequest q = new QueryRequest();
        q.joinType = JoinType.parse(Json.optStr(req, "joinType", "INNER"));
        q.keyPairs = JoinKeyPair.parseKeys(req.get("keys"));

        Object lo = req.get("left");
        Object ro = req.get("right");
        if (lo == null || ro == null) {
            throw new IllegalArgumentException("请求必须包含 'left' 与 'right' 两个关系");
        }
        q.left = Relation.fromJson(lo);
        q.right = Relation.fromJson(ro);

        Object optObj = req.get("options");
        if (optObj != null) {
            Map<String, Object> opt = Json.asObj(optObj, "options");
            q.memoryThresholdRows = Json.optLong(opt, "memoryThresholdRows", q.memoryThresholdRows);
            q.partitions = (int) Json.optLong(opt, "partitions", q.partitions);
            q.diskQuotaBytes = Json.optLong(opt, "diskQuotaBytes", q.diskQuotaBytes);
            q.keepSpillFiles = opt.getOrDefault("keepSpillFiles", false) == Boolean.TRUE;
            q.exportPlan = opt.getOrDefault("exportPlan", true) != Boolean.FALSE;
            q.maxRecursiveLevels = (int) Json.optLong(opt, "maxRecursiveLevels", q.maxRecursiveLevels);
            Object sd = opt.get("spillDir");
            if (sd instanceof String s && !s.isBlank()) q.spillDir = Path.of(s);
        }
        q.validate();
        return q;
    }

    public void validate() {
        if (memoryThresholdRows < 1) {
            throw new IllegalArgumentException("memoryThresholdRows 必须 >= 1");
        }
        if (partitions < 0) {
            throw new IllegalArgumentException("partitions 必须 >= 0（0 表示自动）");
        }
        if (maxRecursiveLevels < 0 || maxRecursiveLevels > 1000) {
            throw new IllegalArgumentException("maxRecursiveLevels 取值范围 [0,1000]");
        }
        // 解析键列，顺带做存在性 / 唯一性校验
        resolveKeyIndices(left, right);
    }

    /** 左右键列的位置索引（同序）。 */
    public int[][] resolveKeyIndices(Relation l, Relation r) {
        int[] li = new int[keyPairs.size()];
        int[] ri = new int[keyPairs.size()];
        for (int i = 0; i < keyPairs.size(); i++) {
            JoinKeyPair p = keyPairs.get(i);
            int a = l.columnIndex(p.leftColumn());
            int b = r.columnIndex(p.rightColumn());
            if (a < 0) {
                throw new IllegalArgumentException(
                        "左关系 '" + l.name() + "' 不存在连接键列 '" + p.leftColumn() + "'");
            }
            if (b < 0) {
                throw new IllegalArgumentException(
                        "右关系 '" + r.name() + "' 不存在连接键列 '" + p.rightColumn() + "'");
            }
            li[i] = a;
            ri[i] = b;
        }
        return new int[][]{li, ri};
    }

    public Map<String, Object> toPlanOptions() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("memoryThresholdRows", memoryThresholdRows);
        m.put("partitions", partitions);
        m.put("diskQuotaBytes", diskQuotaBytes);
        m.put("spillDir", spillDir == null ? System.getProperty("java.io.tmpdir") : spillDir.toString());
        m.put("keepSpillFiles", keepSpillFiles);
        m.put("maxRecursiveLevels", maxRecursiveLevels);
        return m;
    }
}
