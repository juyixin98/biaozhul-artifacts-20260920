package joinopt;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 命令行 / JSON 请求入口。
 *
 * 用法：
 * <pre>
 *   java -cp build/classes joinopt.Main request.json [--export-dir DIR]
 *   cat request.json | java -cp build/classes joinopt.Main -
 * </pre>
 * 标准输出为响应 JSON（见 README 的“请求/响应格式”）。
 * --export-dir DIR 时额外导出 plan.json / data.json / plan.txt / request.json。
 * 任何错误以 exit code=1 + {"error":...} 返回，不输出堆栈给调用方。
 */
public final class Main {

    public static void main(String[] args) throws IOException {
        Path requestFile = null;
        Path exportDir = null;
        for (int i = 0; i < args.length; i++) {
            String a = args[i];
            if ("--export-dir".equals(a)) {
                if (i + 1 >= args.length) fail("--export-dir 缺少目录参数");
                exportDir = Path.of(args[++i]);
            } else if (a.startsWith("--export-dir=")) {
                exportDir = Path.of(a.substring("--export-dir=".length()));
            } else if ("-".equals(a)) {
                requestFile = null;
            } else if (a.startsWith("-")) {
                fail("未知参数: " + a);
            } else {
                requestFile = Path.of(a);
            }
        }

        String text;
        if (requestFile != null) {
            if (!Files.exists(requestFile)) fail("请求文件不存在: " + requestFile);
            text = Files.readString(requestFile, StandardCharsets.UTF_8);
        } else {
            text = new String(System.in.readAllBytes(), StandardCharsets.UTF_8);
        }

        try {
            Object parsed = Json.parse(text);
            Map<String, Object> resp = new Handler().handle(Json.asObj(parsed), exportDir);
            System.out.println(Json.pretty(resp));
        } catch (Exception e) {
            fail(e.getMessage() == null ? e.toString() : e.getMessage());
        }
    }

    private static void fail(String msg) {
        Map<String, Object> err = new LinkedHashMap<>();
        err.put("error", msg);
        System.err.println(Json.pretty(err));
        System.exit(1);
    }

    /** 请求处理器（独立成 public 类方法，方便自动化测试直接调用）。 */
    public static final class Handler {

        public Map<String, Object> handle(Map<String, Object> req, Path exportDir) throws IOException {
            List<String> warnings = new ArrayList<>();

            // ---- 解析表 ----
            List<Object> tablesJson = Json.arr(req, "tables");
            if (tablesJson == null || tablesJson.isEmpty()) {
                throw new IllegalArgumentException("请求缺少 tables 或 tables 为空");
            }
            List<Table> tables = new ArrayList<>();
            Map<String, Integer> nameToIdx = new LinkedHashMap<>();
            for (Object tj : tablesJson) {
                Table t = Table.fromJson(Json.asObj(tj), warnings);
                if (nameToIdx.containsKey(t.name)) {
                    throw new IllegalArgumentException("表名重复: " + t.name);
                }
                nameToIdx.put(t.name, tables.size());
                tables.add(t);
            }
            if (tables.size() > Optimizer.MAX_TABLES) {
                throw new IllegalArgumentException("最多支持 " + Optimizer.MAX_TABLES + " 张表，当前 "
                        + tables.size() + " 张");
            }

            // ---- 解析谓词 ----
            List<JoinPred> preds = new ArrayList<>();
            List<Object> joinsJson = Json.arr(req, "joins");
            if (joinsJson != null) {
                for (int i = 0; i < joinsJson.size(); i++) {
                    JoinPred p = JoinPred.fromJson(joinsJson.get(i), nameToIdx, "#" + (i + 1));
                    // 校验列存在，并顺便确认不是重复边
                    tables.get(p.leftTable).colIndex(p.leftColumn);
                    tables.get(p.rightTable).colIndex(p.rightColumn);
                    preds.add(p);
                }
            }
            if (joinsJson == null) {
                warnings.add("请求未提供 joins，" + tables.size() + " 张表将全部做笛卡尔积");
            }

            // ---- 选项 ----
            Map<String, Object> opt = req.get("options") instanceof Map
                    ? Json.asObj(req.get("options")) : new LinkedHashMap<>();
            boolean wantExec = boolOpt(opt, "execute", true);
            boolean wantEnum = boolOpt(opt, "enumerate", false);
            long limit = opt.containsKey("resultLimit") ? Json.lng(opt, "resultLimit") : 20L;
            if (limit < 0) limit = 20;

            // ---- 优化 ----
            Optimizer optimizer = new Optimizer(tables, preds);
            PlanNode plan = optimizer.bestPlan();

            List<Integer> components = optimizer.connectedComponents();
            boolean cartesian = Optimizer.containsCartesian(plan);
            if (components.size() > 1) {
                warnings.add("连接图不连通：有 " + components.size()
                        + " 个连通分量，完整计划包含笛卡尔积（已在计划中标记 cartesian=true）");
            }

            // ---- 可选：穷举校验 ----
            Map<String, Object> enumJson = null;
            if (wantEnum) {
                if (tables.size() > Enumerator.ENUM_LIMIT) {
                    warnings.add("表数量 > " + Enumerator.ENUM_LIMIT
                            + "，已跳过穷举（计划仍由 DP 给出）");
                } else {
                    Enumerator en = new Enumerator(tables, preds);
                    Enumerator.Report report = en.fullReport();
                    enumJson = report.toJson();
                    enumJson.put("countMatchesFormula",
                            report.totalTrees == report.expectedTreeCount);
                    enumJson.put("dpCost", clean(plan.cost));
                    if (report.minCost != plan.cost) {
                        throw new IllegalStateException("校验失败：穷举最小代价 " + report.minCost
                                + " 与 DP 代价 " + plan.cost + " 不一致");
                    }
                    enumJson.put("matchesDp", true);
                }
            }

            // ---- 可选：实际执行 ----
            Map<String, Object> resultJson = null;
            Rel result = null;
            boolean executed = false;
            if (wantExec) {
                List<String> missing = new ArrayList<>();
                for (Table t : tables) if (t.statsOnly) missing.add(t.name);
                if (!missing.isEmpty()) {
                    warnings.add("以下表仅有统计信息，跳过实际执行: " + missing);
                } else {
                    result = new Executor(tables).execute(plan);
                    executed = true;
                    resultJson = buildResultJson(plan, result, limit);
                }
            }

            // ---- 组装响应 ----
            Map<String, Object> resp = new LinkedHashMap<>();
            resp.put("ok", true);
            resp.put("summary", buildSummary(tables, preds, components, plan, executed));
            resp.put("plan", plan.toJson(tables));
            resp.put("planText", plan.toText(tables).stripTrailing());
            if (enumJson != null) resp.put("enumeration", enumJson);
            if (resultJson != null) resp.put("result", resultJson);
            resp.put("subsets", buildSubsets(tables, optimizer));
            resp.put("warnings", warnings);

            // ---- 导出 ----
            if (exportDir != null) {
                export(exportDir, req, tables, plan, optimizer, result);
                resp.put("exportedTo", exportDir.toAbsolutePath().toString());
            }
            return resp;
        }

        private static boolean boolOpt(Map<String, Object> opt, String key, boolean def) {
            if (!opt.containsKey(key) || opt.get(key) == null) return def;
            Object v = opt.get(key);
            if (v instanceof Boolean) return (Boolean) v;
            return Boolean.parseBoolean(String.valueOf(v));
        }

        private static Map<String, Object> buildSummary(List<Table> tables, List<JoinPred> preds,
                                                        List<Integer> components, PlanNode plan,
                                                        boolean executed) {
            Map<String, Object> s = new LinkedHashMap<>();
            s.put("tableCount", tables.size());
            s.put("joinPredicateCount", preds.size());
            List<Object> comps = new ArrayList<>();
            for (int mask : components) comps.add(maskNames(mask, tables));
            s.put("connectedComponents", comps);
            s.put("connected", components.size() == 1);
            s.put("containsCartesian", Optimizer.containsCartesian(plan));
            s.put("estRows", plan.rounded());
            s.put("totalCost", clean(plan.cost));
            if (executed) {
                s.put("actualRows", plan.actualRows);
                s.put("estimateError",
                        plan.actualRows == 0 ? null
                                : clean((double) (plan.rounded() - plan.actualRows) / plan.actualRows));
            }
            return s;
        }

        private static List<String> maskNames(int mask, List<Table> tables) {
            List<String> names = new ArrayList<>();
            for (int i = 0; i < tables.size(); i++) {
                if (((mask >>> i) & 1) == 1) names.add(tables.get(i).name);
            }
            return names;
        }

        private static Map<String, Object> buildResultJson(PlanNode plan, Rel result, long limit) {
            Map<String, Object> r = new LinkedHashMap<>();
            r.put("columnCount", result.columns.size());
            r.put("columns", result.columns);
            r.put("rowCount", result.rows.size());
            List<Object> sample = new ArrayList<>();
            for (int i = 0; i < Math.min(result.rows.size(), (int) limit); i++) {
                sample.add(result.rows.get(i));
            }
            r.put("rowsShown", sample.size());
            r.put("rows", sample);
            r.put("estRows", plan.rounded());
            Map<String, Object> diff = new LinkedHashMap<>();
            diff.put("estimated", plan.rounded());
            diff.put("actual", plan.actualRows);
            diff.put("absoluteDiff", plan.rounded() - plan.actualRows);
            diff.put("relativeError",
                    plan.actualRows == 0 ? null
                            : clean((double) (plan.rounded() - plan.actualRows) / plan.actualRows));
            r.put("estimateVsActual", diff);
            return r;
        }

        /** 所有非空表子集的估计（展示 DP 的搜索空间与估计的顺序不变性）。 */
        private static List<Object> buildSubsets(List<Table> tables, Optimizer optimizer) {
            Estimator est = optimizer.estimator();
            int full = (1 << tables.size()) - 1;
            List<Object> out = new ArrayList<>();
            for (int mask = 1; mask <= full; mask++) {
                Stats st = est.estimate(mask);
                Map<String, Object> m = new LinkedHashMap<>();
                m.put("tables", maskNames(mask, tables));
                m.put("estRows", Math.round(st.rows));
                out.add(m);
            }
            return out;
        }

        private static void export(Path dir, Map<String, Object> request, List<Table> tables,
                                   PlanNode plan, Optimizer optimizer, Rel result) throws IOException {
            Files.createDirectories(dir);

            // 1) 执行计划（JSON + 文本）
            Map<String, Object> planDoc = new LinkedHashMap<>();
            planDoc.put("plan", plan.toJson(tables));
            planDoc.put("planText", plan.toText(tables).stripTrailing());
            planDoc.put("connectedComponents", componentNames(optimizer, tables));
            Files.writeString(dir.resolve("plan.json"), Json.pretty(planDoc), StandardCharsets.UTF_8);
            Files.writeString(dir.resolve("plan.txt"),
                    plan.toText(tables) + System.lineSeparator(), StandardCharsets.UTF_8);

            // 2) 数据：输入表（统计 + 行）与实际结果
            Map<String, Object> data = new LinkedHashMap<>();
            List<Object> input = new ArrayList<>();
            for (Table t : tables) {
                Map<String, Object> tj = new LinkedHashMap<>();
                tj.put("name", t.name);
                tj.put("columns", t.columns);
                tj.put("rowCount", t.rowCount);
                tj.put("ndv", t.ndv);
                tj.put("statsOnly", t.statsOnly);
                if (!t.statsOnly) tj.put("rows", t.rows);
                input.add(tj);
            }
            data.put("tables", input);
            if (result != null) {
                Map<String, Object> res = new LinkedHashMap<>();
                res.put("columns", result.columns);
                res.put("rowCount", result.rows.size());
                res.put("rows", result.rows);
                data.put("result", res);
            }
            Files.writeString(dir.resolve("data.json"), Json.pretty(data), StandardCharsets.UTF_8);

            // 3) 原始请求回显
            Files.writeString(dir.resolve("request.json"), Json.pretty(request), StandardCharsets.UTF_8);
        }

        private static List<Object> componentNames(Optimizer optimizer, List<Table> tables) {
            List<Object> comps = new ArrayList<>();
            for (int mask : optimizer.connectedComponents()) comps.add(maskNames(mask, tables));
            return comps;
        }

        private static Object clean(double d) {
            if (d == Math.rint(d) && !Double.isInfinite(d)) return (long) d;
            return Math.round(d * 1_000_000d) / 1_000_000d;
        }
    }
}
