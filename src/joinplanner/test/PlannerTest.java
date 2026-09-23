package joinplanner.test;

import java.util.List;

import joinplanner.core.DpResult;
import joinplanner.core.JoinPlanner;
import joinplanner.core.ValidatedProblem;

/** Correctness of the DP against hand-computed costs and brute force. */
public final class PlannerTest {

    public static void run(TestRunner r) {
        r.suite("DP correctness vs hand-computed costs");

        // 2 tables, cards 1000 and 100, selectivity 0.01 => join 1000*100*0.01 = 1000.
        {
            var p = TestBase.builder(1000, 100).edge(0, 1, 0.01).build();
            r.checkClose("2-table cost = join cardinality", TestBase.dpCost(p), 1000.0, 1e-12);
        }

        // Chain 1000 -s.1- 100 -s.5- 10
        // |01| = 1000*100*.1 = 10000 ; |12| = 100*10*.5 = 500 ; |012| = 1000*100*10*.1*.5 = 50000
        // left-deep 0,1,2: 10000 + 50000 = 60000 ; 2,1,0: 500 + 50000 = 50500.
        // The DP searches bushy plans; only the binary tree ((t2 join t1) join t0)
        // reaches 50500 (first join = 500 rows). We assert on that tree shape.
        {
            var p = TestBase.builder(1000, 100, 10).edge(0, 1, 0.1).edge(1, 2, 0.5).build();
            DpResult dp = new JoinPlanner(p).plan();
            r.checkClose("3-chain cost", dp.totalCost(), 50500.0, 1e-9);
            r.check("3-chain: first join is t1-t2 (the small intermediate)",
                    firstJoinTables(dp.root()).containsAll(java.util.List.of("t1", "t2")));
            r.check("3-chain: all leaves present",
                    dp.joinOrderNames().containsAll(java.util.List.of("t0", "t1", "t2")));
        }

        // Star: center t1 small (100), leaves t0=100000 s.01, t2=10000 s.01, t3=1000 s.01
        // Every join passes through the center; check a leaf-first prefix ordering wins.
        {
            var p = TestBase.builder(100000, 100, 10000, 1000)
                    .edge(0, 1, 0.01).edge(1, 2, 0.01).edge(1, 3, 0.01).build();
            DpResult dp = new JoinPlanner(p).plan();
            // best legal orders start center then smallest leaves... verify cost vs
            // independent enumeration instead of hardcoding
            double enumCost = TestBase.bestLegalLeftDeepCost(p);
            r.checkClose("4-star DP <= best left-deep (bushy cannot be worse)",
                    dp.totalCost(), Math.min(dp.totalCost(), enumCost), 1e-9);
            r.check("4-star DP equals/beats best left-deep", dp.totalCost() <= enumCost + 1e-6);
        }

        r.suite("DP exhaustively verified against ALL binary trees (n = 1..6)");
        // Random-ish but deterministic edge/selectivity/cardinality settings; for
        // every case the DP optimum must equal exhaustive recursion over bushy plans.
        long seed = 1234567L;
        int cases = 0;
        for (int n = 1; n <= 6; n++) {
            for (int trial = 0; trial < 8; trial++) {
                double[] cards = new double[n];
                for (int i = 0; i < n; i++) {
                    seed = seed * 6364136223846793005L + 1442695040888963407L;
                    cards[i] = 1 + (seed >>> 58) * 50; // 1..800
                }
                var b = TestBase.builder(cards);
                // build a connected graph: each table i>=1 joins some earlier table
                for (int i = 1; i < n; i++) {
                    seed = seed * 6364136223846793005L + 1442695040888963407L;
                    int j = Math.floorMod(seed >>> 33, i);
                    seed = seed * 6364136223846793005L + 1442695040888963407L;
                    double sel = 0.02 + (seed >>> 60) / 63.0 * 0.9; // 0.02..0.92
                    b.edge(j, i, sel);
                }
                // sometimes add a cycle edge
                if (n >= 4 && trial % 2 == 0) {
                    seed = seed * 6364136223846793005L + 1442695040888963407L;
                    int a = Math.floorMod(seed >>> 33, n);
                    seed = seed * 6364136223846793005L + 1442695040888963407L;
                    int c = Math.floorMod(seed >>> 33, n - 1);
                    if (c >= a) c++;
                    if (!b.hasEdge(a, c)) {
                        b.edge(Math.min(a, c), Math.max(a, c), 0.3);
                    }
                }
                ValidatedProblem p = b.build();
                double dp = TestBase.dpCost(p);
                double brute = TestBase.bruteForceBushyCost(p);
                r.checkClose("n=" + n + " trial=" + trial + " DP==brute-force bushy",
                        dp, brute, 1e-9);
                cases++;
            }
        }
        r.check("ran exhaustive bushy cross-check cases", cases == 48);

        r.suite("DP at n=8 (acceptance boundary)");
        {
            var b = TestBase.builder(50, 200, 5, 800, 30, 900, 2, 70);
            for (int i = 1; i < 8; i++) {
                b.edge(0, i, 0.05 + i * 0.01);
            }
            var p = b.build();
            DpResult dp = new JoinPlanner(p).plan();
            double enumCost = TestBase.bestLegalLeftDeepCost(p);
            r.check("8-table plan found", dp.joinOrderNames().size() == 8);
            r.check("8-table DP not worse than best left-deep",
                    dp.totalCost() <= enumCost + 1e-6);
        }
    }

    /** Tables participating in the lowest join node of the plan tree. */
    private static java.util.Set<String> firstJoinTables(joinplanner.model.PlanNode root) {
        joinplanner.model.PlanNode node = root;
        while (!node.left().isLeaf() || !node.right().isLeaf()) {
            // descend to the join node whose children are both leaves
            if (!node.left().isLeaf()) {
                node = node.left();
            } else {
                node = node.right();
            }
        }
        return java.util.Set.of(
                leafName(node.left()), leafName(node.right()));
    }

    private static String leafName(joinplanner.model.PlanNode leaf) {
        return "t" + leaf.tableIndex();
    }
}
