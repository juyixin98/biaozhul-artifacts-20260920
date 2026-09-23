package joinplanner.sim;

import joinplanner.json.BadInputException;
import joinplanner.json.J;
import joinplanner.model.CostModel;
import joinplanner.model.Edge;
import joinplanner.model.Spec;
import joinplanner.model.Table;
import joinplanner.plan.DpSolution;
import joinplanner.plan.Estimator;
import joinplanner.plan.GraphComponents;
import joinplanner.plan.PlanNode;
import joinplanner.plan.PlanRenderer;
import joinplanner.plan.Planner;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

/**
 * End-to-end scenario runner: generates tables with declared column distributions,
 * plans them with the statistical estimator, then executes the joins for real and
 * replans using measured cardinalities. The divergence between the two plans is the
 * classic "estimated optimum ≠ true optimum" phenomenon under skew / stale stats.
 */
public final class SimulationService {

    public static final long DEFAULT_ROW_CAP = 5_000_000L;
    public static final long MAX_TABLE_ROWS = 200_000L;

    public Map<String, Object> run(Map<String, Object> body) {
        List<Object> tableNodes = J.getArr(body, "tables");
        if (tableNodes == null || tableNodes.isEmpty() || tableNodes.size() > 8) {
            throw new BadInputException("'tables' must contain 1..8 entries");
        }

        int n = tableNodes.size();
        SimTable[] simTables = new SimTable[n];
        Map<String, Integer> indexByName = new HashMap<>();
        for (int i = 0; i < n; i++) {
            Map<String, Object> tn = J.obj(tableNodes.get(i), "tables[" + i + "]");
            String name = J.getStr(tn, "name");
            if (name == null || name.isBlank()) {
                throw new BadInputException("tables[" + i + "].name is required");
            }
            if (indexByName.put(name, i) != null) {
                throw new BadInputException("Duplicate table name: '" + name + "'");
            }
            Object rowsNode = J.get(tn, "rows");
            if (rowsNode == null) {
                throw new BadInputException("tables[" + i + "].rows is required");
            }
            long rows = (long) J.nonNegNum(rowsNode, "tables[" + i + "].rows");
            if (rows > MAX_TABLE_ROWS) {
                throw new BadInputException("tables[" + i + "].rows must be <= " + MAX_TABLE_ROWS
                        + " in simulation (memory bound)");
            }
            List<Object> colNodes = J.getArr(tn, "columns");
            if (colNodes == null) {
                colNodes = List.of();
            }
            ColumnSpec[] cols = new ColumnSpec[colNodes.size()];
            for (int c = 0; c < colNodes.size(); c++) {
                cols[c] = parseColumn(colNodes.get(c), "tables[" + i + "].columns[" + c + "]");
            }
            simTables[i] = new SimTable(name, rows, cols);
        }

        List<SimEdge> simEdges = new ArrayList<>();
        List<Object> edgeNodes = J.getArr(body, "edges");
        if (edgeNodes == null || edgeNodes.isEmpty()) {
            throw new BadInputException("'edges' is required for simulation");
        }
        for (int i = 0; i < edgeNodes.size(); i++) {
            Map<String, Object> en = J.obj(edgeNodes.get(i), "edges[" + i + "]");
            String left = J.getStr(en, "left");
            String right = J.getStr(en, "right");
            if (left == null || right == null) {
                throw new BadInputException("edges[" + i + "] needs 'left' and 'right'");
            }
            Integer li = indexByName.get(left);
            Integer ri = indexByName.get(right);
            if (li == null || ri == null) {
                throw new BadInputException("edges[" + i + "] references unknown table");
            }
            if (li.equals(ri)) {
                throw new BadInputException("self-joins are not supported in simulation");
            }
            int lc = J.getInt(en, "leftColumn") != null ? J.getInt(en, "leftColumn") : 0;
            int rc = J.getInt(en, "rightColumn") != null ? J.getInt(en, "rightColumn") : 0;
            if (lc < 0 || lc >= simTables[li].columns.length) {
                throw new BadInputException("edges[" + i + "].leftColumn out of range for table '" + left + "'");
            }
            if (rc < 0 || rc >= simTables[ri].columns.length) {
                throw new BadInputException("edges[" + i + "].rightColumn out of range for table '" + right + "'");
            }
            Double sel = J.getNum(en, "selectivity");
            if (sel != null && (sel < 0 || sel > 1)) {
                throw new BadInputException("edges[" + i + "].selectivity must be within [0,1]");
            }
            String on = J.getStr(en, "on");
            SimEdge e = new SimEdge(left, right, lc, rc, sel, on);
            e.leftIndex = li;
            e.rightIndex = ri;
            simEdges.add(e);
        }

        long seed = 42;
        Double seedOpt = J.getNum(body, "seed");
        if (seedOpt != null) {
            seed = seedOpt.longValue();
        }
        long rowCap = DEFAULT_ROW_CAP;
        Double capOpt = J.getNum(body, "maxIntermediateRows");
        if (capOpt != null) {
            if (capOpt < 1) {
                throw new BadInputException("maxIntermediateRows must be >= 1");
            }
            rowCap = capOpt.longValue();
        }
        CostModel costModel = CostModel.parse(J.getStr(body, "costModel"));

        // ---- generate data deterministically ----
        Random rng = new Random(seed);
        for (SimTable t : simTables) {
            for (ColumnSpec c : t.columns) {
                c.generated = DataGenerator.generate(c, t.rows, rng);
            }
        }

        // ---- allocate global slots and build the actual executor ----
        int[] degree = new int[n];
        for (SimEdge e : simEdges) {
            degree[e.leftIndex]++;
            degree[e.rightIndex]++;
        }
        int[] slotStart = new int[n];
        int totalSlots = 0;
        for (int t = 0; t < n; t++) {
            slotStart[t] = totalSlots;
            totalSlots += degree[t];
        }
        int[] usedPerTable = new int[n];
        long[][] baseColumn = new long[totalSlots][];
        int[] slotOwner = new int[totalSlots];
        int[][] edgeSlots = new int[simEdges.size()][2];
        SimEdge[] edgeArr = simEdges.toArray(new SimEdge[0]);
        for (int ei = 0; ei < edgeArr.length; ei++) {
            SimEdge e = edgeArr[ei];
            int ls = slotStart[e.leftIndex] + usedPerTable[e.leftIndex]++;
            int rs = slotStart[e.rightIndex] + usedPerTable[e.rightIndex]++;
            edgeSlots[ei][0] = ls;
            edgeSlots[ei][1] = rs;
            slotOwner[ls] = e.leftIndex;
            slotOwner[rs] = e.rightIndex;
            baseColumn[ls] = simTables[e.leftIndex].columns[e.leftColumn].generated;
            baseColumn[rs] = simTables[e.rightIndex].columns[e.rightColumn].generated;
        }
        ActualExecutor executor = new ActualExecutor(n, totalSlots, baseColumn, slotOwner,
                edgeArr, edgeSlots, rowCap);

        // ---- statistical model from measured NDV (uniformity assumption) ----
        List<Table> modelTables = new ArrayList<>();
        for (SimTable t : simTables) {
            modelTables.add(new Table(t.name, t.rows));
        }
        List<Edge> modelEdges = new ArrayList<>();
        List<Map<String, Object>> edgeStats = new ArrayList<>();
        for (int ei = 0; ei < edgeArr.length; ei++) {
            SimEdge e = edgeArr[ei];
            long lndv = DataGenerator.actualNdv(baseColumn[edgeSlots[ei][0]]);
            long rndv = DataGenerator.actualNdv(baseColumn[edgeSlots[ei][1]]);
            boolean lUnique = simTables[e.leftIndex].columns[e.leftColumn].unique;
            boolean rUnique = simTables[e.rightIndex].columns[e.rightColumn].unique;
            Edge me = new Edge(e.left, e.right, e.explicitSelectivity, lndv, rndv,
                    lUnique, rUnique, e.on);
            me.leftIndex = e.leftIndex;
            me.rightIndex = e.rightIndex;
            modelEdges.add(me);

            Map<String, Object> stat = J.newObj();
            stat.put("left", e.left);
            stat.put("right", e.right);
            stat.put("leftNdv", lndv);
            stat.put("rightNdv", rndv);
            edgeStats.add(stat);
        }
        Spec estimatedSpec = new Spec(modelTables, modelEdges, false, costModel, 0.1,
                joinplanner.model.SpecParser.DEFAULT_CAP);
        Estimator estEst = new Estimator(estimatedSpec);
        int fullMask = (1 << n) - 1;
        if (GraphComponents.components(estimatedSpec).size() != 1) {
            throw new BadInputException("Join graph is disconnected; simulation requires one connected component");
        }
        Planner estPlanner = new Planner(estimatedSpec, estEst);
        DpSolution estBest = estPlanner.solve(fullMask);

        // ---- actual cardinalities + actual DP ----
        double[] actualCard = new double[1 << n];
        boolean[] actualCapped = new boolean[1 << n];
        boolean[] actualConnected = new boolean[1 << n];
        List<Map<String, Object>> comparison = new ArrayList<>();
        for (int mask = 1; mask <= fullMask; mask++) {
            if (!estEst.connected(mask)) {
                continue;
            }
            actualConnected[mask] = true;
            ActualExecutor.Actual a = executor.actual(mask);
            actualCard[mask] = a.rows;
            actualCapped[mask] = a.capped;
        }
        ActualCostModel actualModel = new ActualCostModel(actualCard, actualConnected, costModel);
        ActualPlanner actPlanner = new ActualPlanner(actualModel, modelTables, modelEdges);
        ActualPlanner.DpResult actBest = actPlanner.solve(fullMask);

        // ---- evaluate the estimated-optimal tree under actual costs ----
        double estTreeActualCost = evaluateActual(estBest.tree(), actualModel);

        // ---- response ----
        Map<String, Object> resp = J.newObj();
        resp.put("seed", seed);
        resp.put("rowCap", rowCap);
        resp.put("costModel", costModel.name());
        resp.put("columnStatistics", edgeStats);

        Map<String, Object> estOut = J.newObj();
        estOut.put("plan", PlanRenderer.renderPlanTree(estBest.tree(), estimatedSpec));
        estOut.put("steps", PlanRenderer.renderSteps(estBest.tree(), estimatedSpec));
        estOut.put("estimatedTotalCost", PlanRenderer.roundNumber(estBest.cost()));
        estOut.put("leftDeepOrder", PlanRenderer.leftDeepOrder(estBest.tree()));
        resp.put("estimatedOptimal", estOut);

        Map<String, Object> actOut = J.newObj();
        actOut.put("plan", renderActualTree(actBest.tree, modelTables, modelEdges));
        actOut.put("steps", renderActualSteps(actBest.tree, modelTables, modelEdges));
        actOut.put("actualTotalCost", PlanRenderer.roundNumber(actBest.cost));
        actOut.put("leftDeepOrder", actualLeftDeepOrder(actBest.tree, modelTables));
        if (anyCapped(fullMask, actualCapped, estEst)) {
            actOut.put("cappedSubsets", true);
        }
        resp.put("actualOptimal", actOut);

        Map<String, Object> cmp = J.newObj();
        boolean same = SimulationService.canonicalTree(estBest.tree())
                .equals(canonicalActual(actBest.tree));
        cmp.put("plansAgree", same);
        cmp.put("estimatedPlanActualCost", PlanRenderer.roundNumber(estTreeActualCost));
        cmp.put("actualOptimalCost", PlanRenderer.roundNumber(actBest.cost));
        double regret = estTreeActualCost - actBest.cost;
        cmp.put("absoluteRegret", PlanRenderer.roundNumber(regret));
        cmp.put("relativeRegret", actBest.cost > 0
                ? PlanRenderer.roundNumber(regret / actBest.cost) : null);
        resp.put("comparison", cmp);

        for (int mask = 1; mask <= fullMask; mask++) {
            if (!actualConnected[mask]) {
                continue;
            }
            Map<String, Object> row = J.newObj();
            row.put("tables", PlanRenderer.namesIn(mask, estimatedSpec));
            double ev = estEst.card(mask);
            long av = (long) actualCard[mask];
            row.put("estimatedRows", PlanRenderer.roundNumber(ev));
            row.put("actualRows", av);
            row.put("ratioEstimatedOverActual",
                    av == 0 ? null : PlanRenderer.roundNumber(ev / av));
            if (actualCapped[mask]) {
                row.put("actualCapped", true);
            }
            comparison.add(row);
        }
        resp.put("intermediateRows", comparison);
        if (!estimatedSpec.warnings.isEmpty()) {
            resp.put("warnings", estimatedSpec.warnings);
        }
        return resp;
    }

    private ColumnSpec parseColumn(Object node, String where) {
        Map<String, Object> cn = J.obj(node, where);
        String dist = J.getStr(cn, "distribution");
        if (dist == null) {
            throw new BadInputException(where + ".distribution is required");
        }
        Long ndv = null;
        Integer ndvOpt = J.getInt(cn, "ndv");
        if (ndvOpt != null) {
            if (ndvOpt < 1) {
                throw new BadInputException(where + ".ndv must be >= 1");
            }
            ndv = ndvOpt.longValue();
        }
        double skew = 1.0;
        Double skewOpt = J.getNum(cn, "skew");
        if (skewOpt != null) {
            if (skewOpt < 0 || skewOpt > 5) {
                throw new BadInputException(where + ".skew must be within [0,5]");
            }
            skew = skewOpt;
        }
        double hot = 0.5;
        Double hotOpt = J.getNum(cn, "hotFraction");
        if (hotOpt != null) {
            if (hotOpt < 0 || hotOpt > 1) {
                throw new BadInputException(where + ".hotFraction must be within [0,1]");
            }
            hot = hotOpt;
        }
        int offset = 0;
        Integer offOpt = J.getInt(cn, "offset");
        if (offOpt != null) {
            offset = offOpt;
        }
        boolean unique = Boolean.TRUE.equals(J.getBool(cn, "unique"));
        long[] frequencies = null;
        List<Object> freqNodes = J.getArr(cn, "frequencies");
        if (freqNodes != null) {
            frequencies = new long[freqNodes.size()];
            for (int i = 0; i < freqNodes.size(); i++) {
                frequencies[i] = J.nonNegInt(freqNodes.get(i), where + ".frequencies[" + i + "]");
            }
        }
        return new ColumnSpec(dist, ndv == null ? -1 : ndv, skew, hot, frequencies, unique, offset);
    }

    private double evaluateActual(PlanNode node, ActualCostModel model) {
        if (node.leaf()) {
            return 0;
        }
        double nodeCost = switch (model.costModel) {
            case TOTAL_INTERMEDIATE_ROWS -> model.card(node.mask());
            case SUM_OF_INPUTS -> model.card(node.left().mask()) + model.card(node.right().mask());
        };
        return evaluateActual(node.left(), model) + evaluateActual(node.right(), model) + nodeCost;
    }

    private boolean anyCapped(int fullMask, boolean[] capped, Estimator est) {
        for (int mask = 1; mask <= fullMask; mask++) {
            if (est.connected(mask) && capped[mask]) {
                return true;
            }
        }
        return false;
    }

    /** Structural canonicalization ignoring left/right swapping, for plan equality checks. */
    static String canonicalTree(PlanNode node) {
        if (node.leaf()) {
            return "T" + node.tableIndex();
        }
        String l = canonicalTree(node.left());
        String r = canonicalTree(node.right());
        if (l.compareTo(r) > 0) {
            String tmp = l;
            l = r;
            r = tmp;
        }
        return "(" + l + "⋈" + r + ")";
    }

    static String canonicalActual(ActualPlanner.ActualPlanNode node) {
        if (node.leaf) {
            return "T" + node.tableIndex;
        }
        String l = canonicalActual(node.left);
        String r = canonicalActual(node.right);
        if (l.compareTo(r) > 0) {
            String tmp = l;
            l = r;
            r = tmp;
        }
        return "(" + l + "⋈" + r + ")";
    }

    private Map<String, Object> renderActualTree(ActualPlanner.ActualPlanNode node,
                                                 List<Table> tables, List<Edge> edges) {
        Map<String, Object> out = J.newObj();
        out.put("actualRows", node.rows);
        if (node.leaf) {
            out.put("type", "scan");
            out.put("table", tables.get(node.tableIndex).name());
            return out;
        }
        out.put("type", "join");
        out.put("nodeCost", PlanRenderer.roundNumber(node.nodeCost));
        out.put("on", crossingLabels(node.left.mask, node.right.mask, tables, edges));
        out.put("left", renderActualTree(node.left, tables, edges));
        out.put("right", renderActualTree(node.right, tables, edges));
        return out;
    }

    private List<Map<String, Object>> renderActualSteps(ActualPlanner.ActualPlanNode root,
                                                        List<Table> tables, List<Edge> edges) {
        List<Map<String, Object>> steps = new ArrayList<>();
        flattenActual(root, steps, tables, edges,
                new java.util.IdentityHashMap<ActualPlanner.ActualPlanNode, Integer>());
        return steps;
    }

    private void flattenActual(ActualPlanner.ActualPlanNode node, List<Map<String, Object>> steps,
                              List<Table> tables, List<Edge> edges,
                              java.util.IdentityHashMap<ActualPlanner.ActualPlanNode, Integer> stepId) {
        if (node.leaf) {
            return;
        }
        flattenActual(node.left, steps, tables, edges, stepId);
        flattenActual(node.right, steps, tables, edges, stepId);
        int id = steps.size();
        stepId.put(node, id);
        Map<String, Object> step = J.newObj();
        step.put("step", id);
        step.put("operation", "inner join");
        step.put("left", actualRef(node.left, stepId, tables));
        step.put("right", actualRef(node.right, stepId, tables));
        step.put("on", crossingLabels(node.left.mask, node.right.mask, tables, edges));
        step.put("actualOutputRows", node.rows);
        step.put("nodeCost", PlanRenderer.roundNumber(node.nodeCost));
        steps.add(step);
    }

    private Map<String, Object> actualRef(ActualPlanner.ActualPlanNode node,
                                          java.util.IdentityHashMap<ActualPlanner.ActualPlanNode, Integer> stepId,
                                          List<Table> tables) {
        Map<String, Object> ref = J.newObj();
        if (node.leaf) {
            ref.put("kind", "scan");
            ref.put("table", tables.get(node.tableIndex).name());
            ref.put("rows", node.rows);
        } else {
            ref.put("kind", "intermediate");
            ref.put("step", stepId.get(node));
            ref.put("rows", node.rows);
        }
        return ref;
    }

    private List<String> crossingLabels(int l, int r, List<Table> tables, List<Edge> edges) {
        List<String> labels = new ArrayList<>();
        for (Edge e : edges) {
            boolean ll = (l & (1 << e.leftIndex)) != 0;
            boolean lr = (l & (1 << e.rightIndex)) != 0;
            boolean rl = (r & (1 << e.leftIndex)) != 0;
            boolean rr = (r & (1 << e.rightIndex)) != 0;
            if ((ll && rr) || (lr && rl)) {
                labels.add(e.on != null ? e.on : e.left + " = " + e.right);
            }
        }
        return labels;
    }

    private List<String> actualLeftDeepOrder(ActualPlanner.ActualPlanNode node, List<Table> tables) {
        List<String> order = new ArrayList<>();
        ActualPlanner.ActualPlanNode cur = node;
        while (!cur.leaf) {
            if (!cur.right.leaf) {
                return null;
            }
            order.add(0, tables.get(cur.right.tableIndex).name());
            cur = cur.left;
        }
        order.add(0, tables.get(cur.tableIndex).name());
        return order;
    }
}
