package joinplanner.test;

import joinplanner.core.DpResult;
import joinplanner.core.JoinPlanner;
import joinplanner.core.ValidatedProblem;

/** Zero-row tables and cardinality/cost overflow. */
public final class EdgeCaseTest {

    public static void run(TestRunner r) {
        r.suite("Zero-row base tables");
        {
            // empty join: 0 * 1000 * sel = 0
            var p = TestBase.builder(0, 1000).edge(0, 1, 0.5).build();
            DpResult dp = new JoinPlanner(p).plan();
            r.checkClose("empty table => empty join", dp.root().outputRows(), 0.0, 0.0);
            r.checkClose("empty join cost 0", dp.totalCost(), 0.0, 0.0);
        }
        {
            var p = TestBase.builder(10, 0, 20).edge(0, 1, 0.5).edge(1, 2, 0.5).build();
            DpResult dp = new JoinPlanner(p).plan();
            r.checkClose("zero table inside chain => final 0", dp.root().outputRows(), 0.0, 0.0);
            // intermediates: |02| disconnected => not produced; |01|=0, |12|=0, all=0
            r.check("cost stays finite", Double.isFinite(dp.totalCost()));
        }

        r.suite("Cardinality / cost overflow");
        {
            // 6 tables *1e100 rows, edges sel 1 -> intermediates 1e200 .. 1e600
            double huge = 1e100;
            var b = TestBase.builder(huge, huge, huge, huge, huge, huge);
            for (int i = 1; i < 6; i++) {
                b.edge(0, i, 1.0);
            }
            ValidatedProblem p = b.build();
            DpResult dp = new JoinPlanner(p).plan();
            r.check("huge overflow flagged", dp.costOverflow());
            // every join node should still carry a plan (Infinity rendered as null)
            r.checkEq("plan still complete", dp.joinOrderNames().size(), 6);
        }
        {
            // near the boundary: 1e150 * 1e150 * 1e-120 = 1e180 finite
            var p = TestBase.builder(1e150, 1e150).edge(0, 1, 1e-120).build();
            DpResult dp = new JoinPlanner(p).plan();
            r.check("1e180 finite", Double.isFinite(dp.totalCost()));
            r.check("1e180 not flagged", !dp.costOverflow());
        }
        {
            // selectivity that drives product beyond representability
            var p = TestBase.builder(1e200, 1e200, 1e200)
                    .edge(0, 1, 1.0).edge(1, 2, 1.0).build();
            DpResult dp = new JoinPlanner(p).plan();
            r.check("1e600 flagged overflow", dp.costOverflow());
        }

        r.suite("Single-table requests");
        {
            var p = TestBase.builder(1234).build();
            DpResult dp = new JoinPlanner(p).plan();
            r.checkEq("single table order", dp.joinOrderNames(), java.util.List.of("t0"));
            r.checkClose("single table cost 0", dp.totalCost(), 0.0, 0.0);
            r.checkClose("single table rows", dp.root().outputRows(), 1234.0, 1e-12);
        }
    }
}
