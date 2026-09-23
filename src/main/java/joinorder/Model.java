package joinorder;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 解析后的查询模型：表集合、连接图（边）、连通分量。
 *
 * 请求结构见 README。本类只负责装载与校验，不做优化。
 */
public final class Model {

    public static final int MAX_TABLES = 8;

    public final List<Table> tables = new ArrayList<>();
    public final Map<String, Integer> tableIndex = new LinkedHashMap<>();
    /** 无向边列表（索引即边 id）。 */
    public final List<Edge> edges = new ArrayList<>();

    public String queryName;
    public String strategy = "bushy"; // bushy | left-deep
    public boolean execute;
    public long previewLimit = 20;
    public boolean verifyBruteForce;
    public long bruteForceMaxTrees = 200_000;
    /** 用于优化器估计的统计来源：provided | actual。 */
    public String statsSource = "provided";

    // 派生
    private int fullMask;
    private boolean[] connected;
    private List<int[]> components;

    public static Model fromRequest(Map<String, Object> req) {
        Model m = new Model();
        m.queryName = Json.strOr(req, "name", "query");
        m.strategy = Json.strOr(req, "strategy", "bushy");
        if (!m.strategy.equals("bushy") && !m.strategy.equals("left-deep")) {
            throw new EngineException("strategy 只能是 bushy 或 left-deep");
        }
        m.execute = Json.bool(req, "execute", true);
        m.previewLimit = Json.lng(req, "previewLimit", 20);
        m.verifyBruteForce = Json.bool(req, "verifyBruteForce", true);
        m.bruteForceMaxTrees = Json.lng(req, "bruteForceMaxTrees", 200_000);
        m.statsSource = Json.strOr(req, "statsSource", "provided");
        if (!m.statsSource.equals("provided") && !m.statsSource.equals("actual")) {
            throw new EngineException("statsSource 只能是 provided 或 actual");
        }

        List<Object> tableDefs = Json.arr(req, "tables");
        if (tableDefs.isEmpty()) throw new EngineException("请求中必须至少有一张表");
        if (tableDefs.size() > MAX_TABLES) {
            throw new EngineException("最多支持 " + MAX_TABLES + " 张表，当前 " + tableDefs.size() + " 张");
        }
        for (Object td : tableDefs) m.loadTable(Json.asObject(td, "tables[]"));
        m.fullMask = (1 << m.tables.size()) - 1;

        for (Object jd : Json.arr(req, "joins")) m.loadJoin(Json.asObject(jd, "joins[]"));

        m.derive();
        return m;
    }

    private void loadTable(Map<String, Object> td) {
        String tname = Json.str(td, "name");
        if (tableIndex.containsKey(tname)) throw new EngineException("表名重复: " + tname);
        Table t = new Table(tname);
        int idx = tables.size();
        tableIndex.put(tname, idx);
        tables.add(t); // 尽早加入，保证 stats 中 '表名.列名' 解析可用

        // 列名（可选：若有 rows 可自动推导；显式给出用于只有 stats 没有数据的情形）
        Object cols = td.get("columns");
        if (cols instanceof List) {
            for (Object c : (List<?>) cols) t.columns.add(c.toString());
        }

        Object rowsObj = td.get("rows");
        if (rowsObj instanceof List) {
            for (Object ro : (List<?>) rowsObj) {
                Map<String, Object> row = Json.asObject(ro, "tables[" + tname + "].rows[]");
                t.rows.add(new LinkedHashMap<>(row));
                for (String k : row.keySet()) {
                    if (!t.columns.contains(k)) t.columns.add(k);
                }
            }
        }

        Object stats = td.get("stats");
        if (stats instanceof Map) {
            t.providedStats = parseStats(Json.asObject(stats, "tables[" + tname + "].stats"), t);
        }
        t.computeStatsFromData();
    }

    private Stats parseStats(Map<String, Object> st, Table t) {
        Stats s = new Stats();
        Object rc = st.get("rowCount");
        if (!(rc instanceof Number)) throw new EngineException("表 " + t.name + " 的 stats.rowCount 应为数字");
        s.rowCount = ((Number) rc).doubleValue();
        if (s.rowCount < 0) throw new EngineException("表 " + t.name + " 的 rowCount 不能为负");
        Object nd = st.get("ndv");
        if (nd instanceof Map) {
            for (Map.Entry<?, ?> e : ((Map<?, ?>) nd).entrySet()) {
                ColumnRef cr = resolveColumn(e.getKey().toString(), t.name);
                if (!(e.getValue() instanceof Number)) {
                    throw new EngineException("ndv." + e.getKey() + " 应为数字");
                }
                double v = ((Number) e.getValue()).doubleValue();
                if (v < 0) throw new EngineException("ndv 不能为负: " + cr);
                s.ndv.put(cr.canonical, v);
            }
        }
        return s;
    }

    /** "t.c" 或裸列名 "c"（在 defaultTable 内）解析。 */
    public ColumnRef resolveColumn(String text, String defaultTable) {
        if (text.indexOf('.') < 0) {
            if (defaultTable == null) {
                throw new EngineException("列引用 '" + text + "' 缺少表名限定");
            }
            return ColumnRef.of(defaultTable, text);
        }
        ColumnRef cr = ColumnRef.parse(text);
        Integer ti = tableIndex.get(cr.table);
        if (ti == null) throw new EngineException("列引用了不存在的表: " + cr.table);
        Table t = tables.get(ti);
        if (!t.columns.isEmpty() && !t.columns.contains(cr.column)) {
            throw new EngineException("表 " + cr.table + " 不存在列 " + cr.column);
        }
        return cr;
    }

    private void loadJoin(Map<String, Object> jd) {
        List<Object> on = Json.arr(jd, "on");
        if (on.isEmpty()) throw new EngineException("joins[].on 不能为空");
        Edge edge = null;
        for (Object po : on) {
            Map<String, Object> pm = Json.asObject(po, "joins[].on[]");
            ColumnRef l = resolveColumn(Json.str(pm, "left"), null);
            ColumnRef r = resolveColumn(Json.str(pm, "right"), null);
            int a = tableIndex.get(l.table);
            int b = tableIndex.get(r.table);
            if (a == b) throw new EngineException("连接条件必须跨两张表: " + l + " = " + r);
            if (edge == null) {
                edge = findOrCreateEdge(a, b);
            } else if (!(edge.connects(a, b))) {
                throw new EngineException("同一个 joins 条目中的条件必须在相同的两张表之间");
            }
            edge.predicates.add(new EqPredicate(l, r));
        }
    }

    private Edge findOrCreateEdge(int a, int b) {
        if (a > b) { int t = a; a = b; b = t; }
        for (Edge e : edges) if (e.connects(a, b)) return e;
        Edge e = new Edge(a, b);
        edges.add(e);
        return e;
    }

    // ---------------------------------------------------------------- derive

    private void derive() {
        int n = tables.size();
        int total = 1 << n;
        connected = new boolean[total];
        int[] popcount = new int[total];
        for (int mask = 0; mask < total; mask++) popcount[mask] = Integer.bitCount(mask);

        // 单表连通；更大集合连通 <=> 存在一条边连接某单表与其余连通部分
        for (int mask = 1; mask < total; mask++) {
            if (popcount[mask] == 1) { connected[mask] = true; continue; }
            boolean ok = false;
            int bits = mask;
            while (bits != 0) {
                int bit = bits & -bits;
                int rest = mask ^ bit;
                int ti = Integer.numberOfTrailingZeros(bit);
                if (connected[rest] && hasEdgeTo(ti, rest)) { ok = true; break; }
                bits ^= bit;
            }
            connected[mask] = ok;
        }

        // 连通分量（BFS over edges）
        List<int[]> comps = new ArrayList<>();
        int visited = 0;
        while (visited != fullMask) {
            int start = Integer.numberOfTrailingZeros(fullMask ^ visited);
            int cmask = 0;
            int frontier = 1 << start;
            while (frontier != 0) {
                cmask |= frontier;
                int next = 0;
                for (Edge e : edges) {
                    int a = 1 << e.tableA, b = 1 << e.tableB;
                    if ((frontier & a) != 0 && (cmask & b) == 0) next |= b;
                    if ((frontier & b) != 0 && (cmask & a) == 0) next |= a;
                }
                frontier = next;
            }
            comps.add(sortMask(cmask));
            visited |= cmask;
        }
        // 确定性顺序：按分量中最小表索引
        comps.sort((x, y) -> Integer.compare(x[0], y[0]));
        this.components = comps;
    }

    private boolean hasEdgeTo(int tableIdx, int mask) {
        for (Edge e : edges) {
            int other = -1;
            if (e.tableA == tableIdx) other = e.tableB;
            else if (e.tableB == tableIdx) other = e.tableA;
            if (other >= 0 && (mask & (1 << other)) != 0) return true;
        }
        return false;
    }

    private static int[] sortMask(int mask) {
        List<Integer> l = new ArrayList<>();
        int bits = mask;
        while (bits != 0) { int b = bits & -bits; l.add(Integer.numberOfTrailingZeros(b)); bits ^= b; }
        int[] r = new int[l.size()];
        for (int i = 0; i < r.length; i++) r[i] = l.get(i);
        return r;
    }

    // ---------------------------------------------------------------- access

    public int n() { return tables.size(); }
    public int fullMask() { return fullMask; }
    public boolean isConnected(int mask) { return connected[mask]; }
    public List<int[]> components() { return components; }
    public int indexOf(String tableName) {
        Integer i = tableIndex.get(tableName);
        if (i == null) throw new EngineException("未知表: " + tableName);
        return i;
    }
    public Table table(int idx) { return tables.get(idx); }

    /** 跨越两个不相交子计划的所有边。 */
    public List<Edge> crossingEdges(int leftMask, int rightMask) {
        List<Edge> r = new ArrayList<>();
        for (Edge e : edges) if (e.crossesMasks(leftMask, rightMask)) r.add(e);
        return r;
    }

    /** 两张表之间的边（无则 null）。 */
    public Edge edgeBetween(int a, int b) {
        for (Edge e : edges) if (e.connects(a, b)) return e;
        return null;
    }

    /**
     * 某表集合的“边界列”（带缓存）：出现在跨越该集合与外部表的连接边上、
     * 且属于集合一侧的列的规范名（已排序）。这些列的 NDV 是该子计划之上
     * 所有连接唯一可能读取的统计；内部列 NDV 对上层没有影响。
     */
    private List<String>[] boundaryCache;

    @SuppressWarnings("unchecked")
    public List<String> boundaryColumns(int mask) {
        if (boundaryCache == null) {
            int total = 1 << tables.size();
            java.util.Set<String>[] sets = new java.util.Set[total];
            for (int m = 0; m < total; m++) sets[m] = new java.util.LinkedHashSet<>();
            for (Edge e : edges) {
                int ub = 1 << e.tableA, vb = 1 << e.tableB;
                for (EqPredicate p : e.predicates) {
                    ColumnRef ca = p.columnOnSide(ub, this);
                    ColumnRef cb = p.columnOnSide(vb, this);
                    if (ca != null) addBoundary(sets, e.tableA, e.tableB, ca.canonical);
                    if (cb != null) addBoundary(sets, e.tableB, e.tableA, cb.canonical);
                }
            }
            boundaryCache = new List[total];
            for (int m = 0; m < total; m++) {
                List<String> l = new ArrayList<>(sets[m]);
                java.util.Collections.sort(l);
                boundaryCache[m] = l;
            }
        }
        return boundaryCache[mask];
    }

    private void addBoundary(java.util.Set<String>[] sets, int owner, int other, String ownerCol) {
        int ob = 1 << owner, xb = 1 << other;
        for (int m = 0; m < sets.length; m++) {
            if ((m & ob) != 0 && (m & xb) == 0) sets[m].add(ownerCol);
        }
    }
}
