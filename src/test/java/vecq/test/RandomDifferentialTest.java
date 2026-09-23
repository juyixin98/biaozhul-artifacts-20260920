package vecq.test;

import vecq.QueryResult;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/**
 * 随机差分测试（固定种子，可复现）：
 *  随机生成列数据（含随机 NULL）、随机深度的过滤表达式树、随机批大小与随机显式选择
 *  （含重复下标），引擎内部会强制比较向量化与逐行解释器；这里再对响应结构做断言。
 *
 *  两个引擎的过滤算法独立实现（字节数组批处理 vs 递归标量），
 *  任何批边界 / NULL / 三值逻辑分歧都会在这里以 EngineMismatchException 暴露。
 */
public final class RandomDifferentialTest {

    private static final long SEED = 20260923L;

    public static void run() {
        Random rnd = new Random(SEED);
        int rounds = 400;
        for (int round = 0; round < rounds; round++) {
            int n = 1 + rnd.nextInt(18);
            StringBuilder idVals = new StringBuilder();
            for (int i = 0; i < n; i++) {
                if (i > 0) idVals.append(',');
                if (rnd.nextInt(100) < 25) { idVals.append("null"); }
                else { idVals.append(rnd.nextInt(8)); }
            }
            String[] status = {"NEW", "PAID", "X"};
            StringBuilder stVals = new StringBuilder();
            for (int i = 0; i < n; i++) {
                if (i > 0) stVals.append(',');
                if (rnd.nextInt(100) < 25) stVals.append("null");
                else stVals.append('"').append(status[rnd.nextInt(3)]).append('"');
            }

            String table = "{\"name\":\"r\",\"columns\":["
                    + "{\"name\":\"id\",\"type\":\"int\",\"values\":[" + idVals + "]},"
                    + "{\"name\":\"status\",\"type\":\"string\",\"values\":[" + stVals + "]}]}";

            // 随机表达式树（深 0..3），只用普通 and/or/not/compare/isNull（union 单独测）
            String filter = randomFilter(rnd, 3);
            int bs = 1 + rnd.nextInt(Math.max(1, n + 2));

            StringBuilder req = new StringBuilder("{\"table\":").append(table)
                    .append(",\"batchSize\":").append(bs)
                    .append(",\"filter\":").append(filter);

            // 40% 概率叠加含重复的显式 selection
            boolean useSel = rnd.nextInt(100) < 40;
            if (useSel) {
                int k = rnd.nextInt(2 * n + 1);
                StringBuilder sel = new StringBuilder("[");
                for (int i = 0; i < k; i++) {
                    if (i > 0) sel.append(',');
                    sel.append(rnd.nextInt(n));
                }
                sel.append(']');
                req.append(",\"selection\":").append(sel);
            }
            req.append(",\"projection\":[\"id\",\"status\"],\"aggregates\":[\"count(*)\",\"sum(id)\"]}");

            QueryResult result;
            try {
                result = Q.run(req.toString());
            } catch (Throwable t) {
                throw new AssertionError("第 " + round + " 轮随机查询异常: " + t.getMessage()
                        + "\n请求: " + req, t);
            }
            // selectedRows 必须是合法下标且有序
            int[] sv = result.selectionVector().toArray();
            int prev = -1;
            for (int j : sv) {
                if (j < 0 || j >= n) {
                    throw new AssertionError("第 " + round + " 轮：选择向量出现越界下标 " + j);
                }
                if (j < prev) {
                    throw new AssertionError("第 " + round + " 轮：选择向量非有序");
                }
                prev = j;
            }
        }
        Assert.that(true, "400 轮随机差分（向量化 vs 逐行）全部一致");

        // 随机 union（顶层多集合并）差分：引擎内校验向量与逐行一致
        Random r2 = new Random(SEED ^ 0x9e3779b9L);
        for (int round = 0; round < 100; round++) {
            int n = 1 + r2.nextInt(12);
            StringBuilder vals = new StringBuilder();
            for (int i = 0; i < n; i++) {
                if (i > 0) vals.append(',');
                vals.append(i);
            }
            String table = "{\"name\":\"u\",\"columns\":["
                    + "{\"name\":\"id\",\"type\":\"int\",\"values\":[" + vals + "]}]}";
            List<String> branches = new ArrayList<>();
            int bcount = 2 + r2.nextInt(3);
            for (int b = 0; b < bcount; b++) {
                branches.add("{\"column\":\"id\",\"op\":\""
                        + (r2.nextBoolean() ? "<=" : ">=")
                        + "\",\"value\":" + r2.nextInt(n) + "}");
            }
            String req = "{\"table\":" + table
                    + ",\"filter\":{\"op\":\"union\",\"branches\":[" + String.join(",", branches) + "]},"
                    + "\"projection\":[\"id\"],\"aggregates\":[\"count(*)\",\"sum(id)\"]}";
            try {
                Q.run(req);
            } catch (Throwable t) {
                throw new AssertionError("第 " + round + " 轮 union 差分异常: " + t.getMessage()
                        + "\n请求: " + req, t);
            }
        }
        Assert.that(true, "100 轮随机 union 差分（向量化 vs 逐行）全部一致");
    }

    private static String randomFilter(Random rnd, int depth) {
        if (depth == 0 || rnd.nextInt(100) < 40) {
            // 叶子
            int kind = rnd.nextInt(10);
            String col = rnd.nextBoolean() ? "id" : "status";
            if (col.equals("id")) {
                return switch (kind % 4) {
                    case 0 -> "{\"column\":\"id\",\"op\":\"<\",\"value\":" + rnd.nextInt(8) + "}";
                    case 1 -> "{\"column\":\"id\",\"op\":\">=\",\"value\":" + rnd.nextInt(8) + "}";
                    case 2 -> "{\"column\":\"id\",\"op\":\"=\",\"value\":" + rnd.nextInt(8) + "}";
                    default -> "{\"op\":\"isNull\",\"column\":\"id\"}";
                };
            } else {
                if (kind % 3 == 0) return "{\"op\":\"isNull\",\"column\":\"status\"}";
                String op = rnd.nextBoolean() ? "=" : "!=";
                String v = new String[]{"NEW", "PAID", "X"}[rnd.nextInt(3)];
                return "{\"column\":\"status\",\"op\":\"" + op + "\",\"value\":\"" + v + "\"}";
            }
        }
        int op = rnd.nextInt(3);
        if (op == 0) {
            return "{\"op\":\"not\",\"child\":" + randomFilter(rnd, depth - 1) + "}";
        }
        String name = op == 1 ? "and" : "or";
        int k = 2 + rnd.nextInt(2);
        List<String> kids = new ArrayList<>();
        for (int i = 0; i < k; i++) kids.add(randomFilter(rnd, depth - 1));
        return "{\"op\":\"" + name + "\",\"children\":[" + String.join(",", kids) + "]}";
    }
}
