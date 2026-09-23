package joinorder;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 查询引擎门面：解析请求、优化、（可选）暴力校验、（可选）执行、组装 JSON 响应。
 */
public final class Engine {

    public static final String VERSION = "1.0.0";

    public Map<String, Object> run(Map<String, Object> request) {
        Model model = Model.fromRequest(request);
        CostModel cm = new CostModel(model);
        boolean useProvided = model.statsSource.equals("provided");

        // 主优化器
        long t0 = System.nanoTime();
        Object primary;
        String strategyUsed;
        Optimizer.Result bushy = null;
        LeftDeepOptimizer.Result leftDeep = null;
        if (model.strategy.equals("left-deep")) {
            LeftDeepOptimizer ld = new LeftDeepOptimizer(model, cm, useProvided);
            leftDeep = ld.optimize();
            primary = leftDeep;
            strategyUsed = "left-deep";
        } else {
            Optimizer opt = new Optimizer(model, cm, useProvided);
            bushy = opt.optimize();
            primary = bushy;
            strategyUsed = "bushy";
        }
        long optMicros = (System.nanoTime() - t0) / 1000;

        PlanNode plan = primary instanceof Optimizer.Result
                ? ((Optimizer.Result) primary).plan
                : ((LeftDeepOptimizer.Result) primary).plan;
        long candidates = primary instanceof Optimizer.Result
                ? ((Optimizer.Result) primary).candidatesConsidered
                : ((LeftDeepOptimizer.Result) primary).candidatesConsidered;

        // 对照：同时算出另一种策略的代价（仅对比，不执行）
        LeftDeepOptimizer.Result ldCompare = null;
        Optimizer.Result bushyCompare = null;
        if (model.strategy.equals("bushy")) {
            ldCompare = new LeftDeepOptimizer(model, cm, useProvided).optimize();
        } else {
            bushyCompare = new Optimizer(model, cm, useProvided).optimize();
        }

        // 暴力枚举校验（允许笛卡尔积的全空间最小代价）
        Map<String, Object> verify = null;
        if (model.verifyBruteForce) {
            BruteForce bf = new BruteForce(model, cm, useProvided, model.bruteForceMaxTrees);
            BruteForce.Report rep = bf.enumerate();
            verify = new LinkedHashMap<>();
            verify.put("performed", rep.run);
            verify.put("capped", rep.capped);
            verify.put("legalPlanCount", rep.planCount);
            verify.put("maxTrees", model.bruteForceMaxTrees);
            if (rep.run) {
                verify.put("bruteForceMinCost", PlanNode.round(rep.minCost));
                verify.put("dpCost", PlanNode.round(plan.estimatedCost));
                double diff = Math.abs(rep.minCost - plan.estimatedCost);
                verify.put("costDifference", PlanNode.round(diff));
                // 允许相对 1e-9：两条递归路径上 min/连乘的浮点舍入可在末位累积
                double tol = Math.max(1e-6, Math.max(rep.minCost, plan.estimatedCost) * 1e-9);
                verify.put("matchesMinimum", diff <= tol);
                verify.put("optimalTopSplits", rep.optimalSplits);
                verify.put("bruteForceTopRows", PlanNode.round(rep.topRows));
            } else {
                verify.put("reason", "合法计划数超过上限 bruteForceMaxTrees="
                        + model.bruteForceMaxTrees + "，跳过穷举（DP 仍正常执行）");
            }
        }

        // 执行
        Map<String, Object> execution = null;
        List<String> warnings = new ArrayList<>();
        if (bushy != null) warnings.addAll(bushy.warnings);
        if (model.execute) {
            checkExecutable(model, warnings);
            Executor ex = new Executor(model);
            long e0 = System.nanoTime();
            Executor.Result er = ex.execute(plan);
            long execMicros = (System.nanoTime() - e0) / 1000;

            execution = new LinkedHashMap<>();
            execution.put("executed", true);
            execution.put("elapsedMicros", execMicros);
            execution.put("resultRowCount", er.rows.size());

            // 估计 vs 实际 逐节点对比
            List<Map<String, Object>> diffs = new ArrayList<>();
            collectDiffs(model, plan, diffs);
            execution.put("estimateVsActual", diffs);

            Stats topActual = plan.actual;
            Stats topEst = plan.estimated;
            Map<String, Object> top = new LinkedHashMap<>();
            top.put("estimatedRows", PlanNode.round(topEst.rowCount));
            top.put("actualRows", (long) topActual.rowCount);
            top.put("estimatedCost", PlanNode.round(plan.estimatedCost));
            top.put("actualCost", PlanNode.round(plan.actualCost));
            top.put("rowRatio", topActual.rowCount == 0
                    ? null : PlanNode.round(topEst.rowCount / topActual.rowCount));
            execution.put("root", top);

            // 结果预览
            long limit = Math.max(0, model.previewLimit);
            List<Object> preview = new ArrayList<>();
            for (int i = 0; i < Math.min((int) limit, er.rows.size()); i++) {
                preview.add(stripQualifiers(er.rows.get(i)));
            }
            execution.put("preview", preview);
            if (er.rows.size() > limit) {
                execution.put("previewTruncated", true);
                warnings.add("结果共 " + er.rows.size() + " 行，仅预览前 " + limit + " 行");
            }
        }

        // DP 表导出
        Map<String, Object> dpDump = dumpMemo(model, bushy != null ? bushy.memo : null,
                leftDeep != null ? leftDeep.memo : null);

        // 响应
        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("engine", "join-order-optimizer");
        resp.put("version", VERSION);
        resp.put("query", model.queryName);
        resp.put("strategy", strategyUsed);
        resp.put("statsSource", model.statsSource);
        resp.put("tableCount", model.n());
        resp.put("edgeCount", model.edges.size());
        resp.put("connectedComponents", model.components().size());
        resp.put("optimizerElapsedMicros", optMicros);
        resp.put("candidatesConsidered", candidates);
        if (primary instanceof Optimizer.Result) {
            resp.put("maxCandidatesPerSubproblem",
                    ((Optimizer.Result) primary).maxCandidatesPerSubproblem);
        }
        resp.put("plan", plan.toMap(model));
        resp.put("planText", plan.toTree(model).stripTrailing());
        resp.put("dpTable", dpDump);

        // 策略对照
        Map<String, Object> cmp = new LinkedHashMap<>();
        cmp.put("bushyCost",
                PlanNode.round(bushy != null ? bushy.plan.estimatedCost : bushyCompare.plan.estimatedCost));
        cmp.put("leftDeepCost",
                PlanNode.round(ldCompare != null ? ldCompare.plan.estimatedCost : leftDeep.plan.estimatedCost));
        double bushyC = bushy != null ? bushy.plan.estimatedCost : bushyCompare.plan.estimatedCost;
        double leftC = ldCompare != null ? ldCompare.plan.estimatedCost : leftDeep.plan.estimatedCost;
        cmp.put("bushyWinsBy", PlanNode.round(leftC - bushyC));
        resp.put("strategyComparison", cmp);

        if (verify != null) resp.put("bruteForceVerification", verify);
        if (execution != null) resp.put("execution", execution);
        if (!warnings.isEmpty()) resp.put("warnings", warnings);
        return resp;
    }

    private void checkExecutable(Model model, List<String> warnings) {
        List<String> missing = new ArrayList<>();
        for (Table t : model.tables) {
            if (t.rows.isEmpty()) missing.add(t.name);
        }
        if (!missing.isEmpty()) {
            throw new EngineException("以下表没有内联数据（rows 为空），无法实际执行；"
                    + "如需只做代价估计请设置 execute=false：" + missing);
        }
        if (model.statsSource.equals("provided")) {
            for (Table t : model.tables) {
                if (t.providedStats != null) {
                    warnings.add("表 " + t.name + " 的优化器使用了请求中提供的统计（可能与真实数据不同，"
                            + "用于演示估计偏差）；执行始终基于真实数据");
                }
            }
        }
    }

    private void collectDiffs(Model model, PlanNode p, List<Map<String, Object>> out) {
        if (p instanceof JoinNode) {
            JoinNode j = (JoinNode) p;
            collectDiffs(model, j.left, out);
            collectDiffs(model, j.right, out);
        }
        if (p.actual != null) {
            Map<String, Object> d = new LinkedHashMap<>();
            d.put("node", p.nodeType());
            d.put("tables", p.tableNames(model));
            d.put("estimatedRows", PlanNode.round(p.estimated.rowCount));
            d.put("actualRows", (long) p.actual.rowCount);
            d.put("ratio", p.actual.rowCount == 0 ? null
                    : PlanNode.round(p.estimated.rowCount / p.actual.rowCount));
            d.put("cartesian", p.cartesian);
            out.add(d);
        }
    }

    private Map<String, Object> dumpMemo(Model model,
                                         Map<Integer, OptEntry> bushyMemo,
                                         Map<Integer, OptEntry> ldMemo) {
        Map<Integer, OptEntry> memo = bushyMemo != null ? bushyMemo : ldMemo;
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("kind", bushyMemo != null ? "bushy-dp (subset)" : "left-deep-dp (prefix-set)");
        List<Object> entries = new ArrayList<>();
        for (OptEntry e : memo.values()) entries.add(e.dump(model));
        d.put("entries", entries);
        d.put("entryCount", entries.size());
        return d;
    }

    private Map<String, Object> stripQualifiers(Map<String, Object> qualified) {
        Map<String, Object> bare = new LinkedHashMap<>();
        for (Map.Entry<String, Object> e : qualified.entrySet()) {
            String k = e.getKey();
            int idx = k.indexOf('.');
            bare.put(idx >= 0 ? k.substring(idx + 1) : k, e.getValue());
        }
        return bare;
    }
}
