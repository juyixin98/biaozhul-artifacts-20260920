package phj;

import phj.json.Json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * 样例数据生成器：生成一份可直接提交的查询请求 JSON。
 *
 * 用法：
 *   java phj.DataGen &lt;leftRows&gt; &lt;rightRows&gt; &lt;distinctKeys&gt; &lt;nullPct&gt; &lt;joinType&gt; [seed]
 * 例：
 *   java phj.DataGen 2000 1000 5 10 INNER 42
 *
 * distinctKeys=1 即“全热点键”场景；nullPct 为含 NULL 键行的百分比（0-100）。
 * 每行带一个负载列 payload，用于验证整行正确拼接。
 */
public final class DataGen {

    private DataGen() {}

    public static void main(String[] args) {
        if (args.length < 5) {
            System.err.println("用法: DataGen <leftRows> <rightRows> <distinctKeys> <nullPct> <joinType> [seed]");
            System.exit(2);
        }
        int ln = Integer.parseInt(args[0]);
        int rn = Integer.parseInt(args[1]);
        int dk = Math.max(1, Integer.parseInt(args[2]));
        int nullPct = Math.max(0, Math.min(100, Integer.parseInt(args[3])));
        String joinType = args[4];
        long seed = args.length > 5 ? Long.parseLong(args[5]) : 1L;
        Random rnd = new Random(seed);

        Map<String, Object> req = new LinkedHashMap<>();
        req.put("joinType", joinType);
        req.put("keys", List.of("k"));
        req.put("left", buildRelation("L", ln, dk, nullPct, rnd, "lv"));
        req.put("right", buildRelation("R", rn, dk, nullPct, rnd, "rv"));
        Map<String, Object> opt = new LinkedHashMap<>();
        opt.put("memoryThresholdRows", 8);
        opt.put("diskQuotaBytes", -1);
        opt.put("exportPlan", true);
        req.put("options", opt);
        System.out.println(Json.writePretty(req));
    }

    private static Map<String, Object> buildRelation(String name, int n, int distinctKeys,
                                                     int nullPct, Random rnd, String payloadPrefix) {
        Map<String, Object> rel = new LinkedHashMap<>();
        rel.put("name", name);
        rel.put("columns", List.of("id", "k", payloadPrefix));
        List<Object> rows = new ArrayList<>(n);
        for (int i = 0; i < n; i++) {
            Object key;
            if (rnd.nextInt(100) < nullPct) {
                key = null;
            } else {
                key = (long) rnd.nextInt(distinctKeys);
            }
            // 用 Arrays.asList 而非 List.of：行允许包含 null（NULL 键）
            rows.add(java.util.Arrays.asList((Object) (long) i, key, payloadPrefix + "-" + i));
        }
        rel.put("rows", rows);
        return rel;
    }
}
