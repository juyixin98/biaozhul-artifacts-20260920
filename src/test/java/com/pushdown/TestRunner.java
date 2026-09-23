package com.pushdown;

import com.pushdown.api.JsonPlan;
import com.pushdown.api.Server;
import com.pushdown.exec.Engine;
import com.pushdown.expr.Expr;
import com.pushdown.json.Json;
import com.pushdown.opt.Optimizer;
import com.pushdown.plan.Plan;
import com.sun.net.httpserver.HttpServer;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

/**
 * Test runner (plain main, no test framework). Exits non-zero on any failure.
 *
 * Includes the acceptance exhaustive check: enumerate all small tables over
 * the domain {NULL,1,2} (every subset of up to 3 rows of the 9 possible
 * 2-column rows), cross them, and for INNER and LEFT joins compare the
 * original plan result against the rewritten plan result for a battery of
 * predicates covering right-table NULL filters, duplicate column names and
 * constant-false predicates.
 */
public final class TestRunner {

    static int passed = 0, failed = 0;
    static final List<String> failures = new ArrayList<>();

    static void check(boolean cond, String msg) {
        if (cond) passed++;
        else {
            failed++;
            failures.add(msg);
            System.out.println("FAIL: " + msg);
        }
    }

    public static void main(String[] args) throws Exception {
        testJson();
        testEval();
        testNullRejection();
        testPushdownStructure();
        testExhaustive();
        testHttp();
        System.out.printf("%npassed=%d failed=%d%n", passed, failed);
        if (failed > 0) {
            System.out.println("failures:");
            failures.forEach(f -> System.out.println("  - " + f));
            System.exit(1);
        }
        System.out.println("ALL TESTS PASSED");
    }

    // ------------------------------------------------------------------ JSON

    static void testJson() {
        String s = "{\"a\":[1,2.5,null,\"x\"],\"b\":{\"c\":true}}";
        Object v = Json.parse(s);
        check(Json.write(v).equals(s), "json round trip: " + Json.write(v));
        Object n = Json.parse("{\"lit\":null}");
        check(Json.write(n).equals("{\"lit\":null}"), "json null round trip");
    }

    // ------------------------------------------------------------------ eval

    static void testEval() {
        List<Expr.Col> schema = List.of(new Expr.Col("T", "A"));
        Object[] rowNull = {null};
        Object[] rowOne = {1L};

        Expr eq1 = new Expr.Bin("=", new Expr.Col("T", "A"), new Expr.Lit(1L));
        check(Expr.eval(eq1, schema, rowNull) == null, "1 = NULL is UNKNOWN");
        check(Boolean.TRUE.equals(Expr.eval(eq1, schema, rowOne)), "1 = 1 is TRUE");

        Expr and = new Expr.And(new Expr.Lit(false), new Expr.Col("T", "A"));
        check(Boolean.FALSE.equals(Expr.eval(and, schema, rowNull)), "FALSE AND NULL is FALSE");

        Expr or = new Expr.Or(new Expr.Lit(true), new Expr.Col("T", "A"));
        check(Boolean.TRUE.equals(Expr.eval(or, schema, rowNull)), "TRUE OR NULL is TRUE");

        Expr isNull = new Expr.IsNull(new Expr.Col("T", "A"));
        check(Boolean.TRUE.equals(Expr.eval(isNull, schema, rowNull)), "NULL IS NULL is TRUE");

        Expr notNull = new Expr.Not(new Expr.IsNull(new Expr.Col("T", "A")));
        check(Boolean.FALSE.equals(Expr.eval(notNull, schema, rowNull)), "NOT(NULL IS NULL) is FALSE");
    }

    // --------------------------------------------------------- NULL-rejection

    static void testNullRejection() {
        Set<Expr.Col> right = Set.of(new Expr.Col("U", "C"));
        Expr.Col uc = new Expr.Col("U", "C");
        check(Optimizer.nullRejecting(new Expr.Bin("=", uc, new Expr.Lit(1L)), right),
                "U.C = 1 is null-rejecting");
        check(!Optimizer.nullRejecting(new Expr.IsNull(uc), right),
                "U.C IS NULL is not null-rejecting");
        check(Optimizer.nullRejecting(new Expr.IsNotNull(uc), right),
                "U.C IS NOT NULL is null-rejecting");
        check(!Optimizer.nullRejecting(
                new Expr.Or(new Expr.IsNull(uc), new Expr.Bin("=", uc, new Expr.Lit(1L))), right),
                "U.C IS NULL OR U.C = 1 is not null-rejecting");
        check(Optimizer.nullRejecting(
                new Expr.And(new Expr.IsNull(uc), new Expr.Bin("=", uc, new Expr.Lit(1L))), right),
                "U.C IS NULL AND U.C = 1 is null-rejecting (contradiction)");
        check(Optimizer.nullRejecting(new Expr.Lit(false), right),
                "constant FALSE is (vacuously) null-rejecting");
        check(!Optimizer.nullRejecting(new Expr.Lit(true), right),
                "constant TRUE is not null-rejecting");
    }

    // ---------------------------------------------------- pushdown structure

    static Map<String, Engine.Table> catalog() {
        Map<String, Engine.Table> cat = new LinkedHashMap<>();
        cat.put("T", new Engine.Table(List.of("A", "B"), List.of(
                new Object[]{1L, 1L}, new Object[]{2L, null})));
        cat.put("U", new Engine.Table(List.of("B", "C"), List.of(
                new Object[]{1L, 1L}, new Object[]{null, null})));
        return cat;
    }

    static Plan.Join join(Plan.JoinType type) {
        return new Plan.Join(type, new Plan.Scan("T"), new Plan.Scan("U"),
                new Expr.Bin("=", new Expr.Col("T", "B"), new Expr.Col("U", "B")));
    }

    static boolean hasReason(Optimizer.Result r, String rule) {
        return r.reasons().stream().anyMatch(x -> x.rule().equals(rule));
    }

    static boolean filterAboveJoin(Plan p) {
        // true if the top of the (possibly Project-wrapped) plan is a Filter above a Join
        if (p instanceof Plan.Project pr) return filterAboveJoin(pr.child());
        return p instanceof Plan.Filter f && stripFilters(f.child()) instanceof Plan.Join;
    }

    static Plan stripFilters(Plan p) {
        while (p instanceof Plan.Filter f) p = f.child();
        return p;
    }

    static void testPushdownStructure() {
        Map<String, Engine.Table> cat = catalog();

        // 1. LEFT JOIN + right-side NULL-rejecting predicate -> pushed into right input.
        Expr ucEq1 = new Expr.Bin("=", new Expr.Col("U", "C"), new Expr.Lit(1L));
        Plan p1 = new Plan.Filter(ucEq1, join(Plan.JoinType.LEFT));
        Optimizer.Result r1 = Optimizer.optimize(p1, cat);
        check(hasReason(r1, "push-right-null-rejecting"), "left join: U.C=1 pushed with null-rejecting reason");
        check(!filterAboveJoin(r1.plan()), "left join: U.C=1 no filter left above join");
        check(stripFilters(r1.plan()) instanceof Plan.Join j && j.type() == Plan.JoinType.INNER,
                "left join: null-rejecting predicate strengthens join to INNER");

        // 2. LEFT JOIN + right-side non-null-rejecting predicate -> stays above.
        Expr ucIsNull = new Expr.IsNull(new Expr.Col("U", "C"));
        Plan p2 = new Plan.Filter(ucIsNull, join(Plan.JoinType.LEFT));
        Optimizer.Result r2 = Optimizer.optimize(p2, cat);
        check(hasReason(r2, "kept-for-null-preservation"), "left join: U.C IS NULL kept above with reason");
        check(filterAboveJoin(r2.plan()), "left join: U.C IS NULL filter remains above join");

        // 3. INNER JOIN + right-side predicate -> pushed.
        Plan p3 = new Plan.Filter(ucEq1, join(Plan.JoinType.INNER));
        Optimizer.Result r3 = Optimizer.optimize(p3, cat);
        check(hasReason(r3, "push-right-inner"), "inner join: U.C=1 pushed right");

        // 4. LEFT JOIN + left-side predicate -> pushed left.
        Expr taEq1 = new Expr.Bin("=", new Expr.Col("T", "A"), new Expr.Lit(1L));
        Plan p4 = new Plan.Filter(taEq1, join(Plan.JoinType.LEFT));
        Optimizer.Result r4 = Optimizer.optimize(p4, cat);
        check(hasReason(r4, "push-left"), "left join: T.A=1 pushed left");
        check(!filterAboveJoin(r4.plan()), "left join: T.A=1 no filter left above join");

        // 5. Constant FALSE on LEFT JOIN -> kept above (must not fabricate preserved rows).
        Plan p5 = new Plan.Filter(new Expr.Lit(false), join(Plan.JoinType.LEFT));
        Optimizer.Result r5 = Optimizer.optimize(p5, cat);
        check(hasReason(r5, "constant-kept-above-left-join"), "left join: FALSE kept above");
        check(filterAboveJoin(r5.plan()), "left join: FALSE filter remains above join");
        check(Engine.execute(r5.plan(), cat).rows().isEmpty(), "left join: FALSE yields empty result");

        // 6. Constant FALSE on INNER JOIN -> pushed to left input, result empty.
        Plan p6 = new Plan.Filter(new Expr.Lit(false), join(Plan.JoinType.INNER));
        Optimizer.Result r6 = Optimizer.optimize(p6, cat);
        check(hasReason(r6, "constant-pushed-inner"), "inner join: FALSE pushed");
        check(Engine.execute(r6.plan(), cat).rows().isEmpty(), "inner join: FALSE yields empty result");

        // 7. Two-side predicate stays above either join type.
        Expr cross = new Expr.Bin("=", new Expr.Col("T", "A"), new Expr.Col("U", "C"));
        Plan p7 = new Plan.Filter(cross, join(Plan.JoinType.LEFT));
        Optimizer.Result r7 = Optimizer.optimize(p7, cat);
        check(hasReason(r7, "kept-above-join"), "two-side predicate kept above join");

        // 8. Constant-folding of 1 = 0.
        Plan p8 = new Plan.Filter(new Expr.Bin("=", new Expr.Lit(1L), new Expr.Lit(0L)),
                join(Plan.JoinType.INNER));
        Optimizer.Result r8 = Optimizer.optimize(p8, cat);
        check(hasReason(r8, "constant-fold"), "1 = 0 folded to constant");
        check(Engine.execute(r8.plan(), cat).rows().isEmpty(), "1 = 0 yields empty result");
    }

    // ------------------------------------------------- exhaustive acceptance

    static List<Expr> predicates() {
        Expr.Col ta = new Expr.Col("T", "A");
        Expr.Col uc = new Expr.Col("U", "C");
        Expr.Lit one = new Expr.Lit(1L);
        Expr.Lit two = new Expr.Lit(2L);
        List<Expr> ps = new ArrayList<>();
        ps.add(new Expr.Bin("=", ta, one));                                   // left only
        ps.add(new Expr.Bin("=", uc, one));                                   // right, null-rejecting
        ps.add(new Expr.IsNull(uc));                                          // right, NOT null-rejecting
        ps.add(new Expr.Bin("=", ta, uc));                                    // both sides
        ps.add(new Expr.Lit(false));                                          // constant false
        ps.add(new Expr.And(new Expr.Bin("=", ta, one), new Expr.Bin("=", uc, one))); // conjunction
        ps.add(new Expr.IsNotNull(uc));                                       // right, null-rejecting
        ps.add(new Expr.Or(new Expr.Bin("=", uc, one), new Expr.Bin("=", ta, two))); // both sides
        ps.add(new Expr.Bin("=", one, new Expr.Lit(0L)));                     // constant false via folding
        ps.add(new Expr.Or(new Expr.IsNull(uc), new Expr.Bin("=", uc, one)));// right, NOT null-rejecting
        return ps;
    }

    /** All tables with columns (X,Y) over domain {null,1,2}, of size 0..3. */
    static List<Engine.Table> allSmallTables() {
        List<Object[]> domain = new ArrayList<>();
        List<Object> vals = Arrays.asList(null, 1L, 2L);
        for (Object a : vals) for (Object b : vals) domain.add(new Object[]{a, b});
        List<Engine.Table> tables = new ArrayList<>();
        int n = domain.size();
        // all subsets of size 0..3 encoded as bitmasks
        for (int mask = 0; mask < (1 << n); mask++) {
            if (Integer.bitCount(mask) > 3) continue;
            List<Object[]> rows = new ArrayList<>();
            for (int i = 0; i < n; i++) if ((mask & (1 << i)) != 0) rows.add(domain.get(i));
            tables.add(new Engine.Table(List.of("X", "Y"), rows));
        }
        return tables;
    }

    static Plan buildPlan(Expr pred, Plan.JoinType type) {
        // Duplicate column names on purpose: T.B and U.B.
        Plan.Join j = new Plan.Join(type, new Plan.Scan("T"), new Plan.Scan("U"),
                new Expr.Bin("=", new Expr.Col("T", "B"), new Expr.Col("U", "B")));
        Plan.Filter f = new Plan.Filter(pred, j);
        return new Plan.Project(List.of(
                new Plan.Item(new Expr.Col("T", "A"), "a"),
                new Plan.Item(new Expr.Col("T", "B"), "t_b"),
                new Plan.Item(new Expr.Col("U", "B"), "u_b"),
                new Plan.Item(new Expr.Col("U", "C"), "c")), f);
    }

    static String canonical(Engine.Output out) {
        TreeSet<String> rows = new TreeSet<>();
        for (List<Object> r : out.rows()) rows.add(String.valueOf(r));
        return out.schema() + " " + rows;
    }

    static void testExhaustive() {
        List<Engine.Table> tables = allSmallTables();
        List<Expr> preds = predicates();
        long combos = 0, mismatches = 0;
        for (Engine.Table t : tables) {
            for (Engine.Table u : tables) {
                Map<String, Engine.Table> cat = new LinkedHashMap<>();
                cat.put("T", new Engine.Table(List.of("A", "B"), t.rows()));
                cat.put("U", new Engine.Table(List.of("B", "C"), u.rows()));
                for (Plan.JoinType jt : Plan.JoinType.values()) {
                    for (Expr pred : preds) {
                        Plan original = buildPlan(pred, jt);
                        Optimizer.Result opt = Optimizer.optimize(original, cat);
                        String before = canonical(Engine.execute(original, cat));
                        String after = canonical(Engine.execute(opt.plan(), cat));
                        combos++;
                        if (!before.equals(after)) {
                            mismatches++;
                            if (mismatches <= 5) {
                                System.out.println("MISMATCH join=" + jt + " pred=" + Optimizer.show(pred));
                                System.out.println("  T=" + rowsOf(cat.get("T")) + " U=" + rowsOf(cat.get("U")));
                                System.out.println("  before=" + before);
                                System.out.println("  after =" + after);
                            }
                        }
                    }
                }
            }
        }
        System.out.printf("exhaustive: %d table-pairs x %d join types x %d predicates = %d comparisons, mismatches=%d%n",
                (long) tables.size() * tables.size(), Plan.JoinType.values().length, preds.size(), combos, mismatches);
        check(mismatches == 0, "exhaustive rewrite equivalence: " + mismatches + " mismatches");
        check(tables.size() == 130, "expected 130 small tables, got " + tables.size());
    }

    static String rowsOf(Engine.Table t) {
        StringBuilder sb = new StringBuilder("[");
        for (Object[] r : t.rows()) sb.append(Arrays.toString(r)).append(";");
        return sb.append("]").toString();
    }

    // ------------------------------------------------------------------ HTTP

    static void testHttp() throws Exception {
        HttpServer server = Server.create(0);
        server.start();
        int port = server.getAddress().getPort();
        try {
            String body = """
                {
                  "tables": {
                    "T": {"columns": ["A","B"], "rows": [[1,1],[2,2],[3,null]]},
                    "U": {"columns": ["B","C"], "rows": [[1,10],[2,null]]}
                  },
                  "plan": {
                    "type": "filter",
                    "pred": {"op":"=", "args":[{"col":"U.C"}, {"lit":10}]},
                    "child": {
                      "type": "join", "joinType": "left",
                      "left": {"type":"scan","table":"T"},
                      "right": {"type":"scan","table":"U"},
                      "on": {"op":"=", "args":[{"col":"T.B"},{"col":"U.B"}]}
                    }
                  }
                }
                """;
            HttpClient client = HttpClient.newHttpClient();
            HttpRequest req = HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/query"))
                    .POST(HttpRequest.BodyPublishers.ofString(body))
                    .header("Content-Type", "application/json").build();
            HttpResponse<String> resp = client.send(req, HttpResponse.BodyHandlers.ofString());
            check(resp.statusCode() == 200, "http status 200, got " + resp.statusCode());
            Map<String, Object> m = Json.parseObject(resp.body());
            check(m.containsKey("optimizedPlan") && m.containsKey("reasons"),
                    "http response has optimizedPlan and reasons");
            @SuppressWarnings("unchecked")
            List<Object> rows = (List<Object>) m.get("rows");
            check(rows != null && rows.size() == 1, "http query returns 1 row, got " + rows);
            @SuppressWarnings("unchecked")
            List<Object> reasons = (List<Object>) m.get("reasons");
            check(reasons.toString().contains("push-right-null-rejecting"),
                    "http reasons mention push-right-null-rejecting");
        } finally {
            server.stop(0);
        }
    }
}
