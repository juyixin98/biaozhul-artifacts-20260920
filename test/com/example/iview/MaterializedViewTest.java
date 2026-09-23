package com.example.iview;

import com.example.iview.model.OrderLine;
import com.example.iview.model.Product;
import com.example.iview.view.ApplyResult;
import com.example.iview.view.CategoryAggregate;
import com.example.iview.view.MaterializedView;

import java.math.BigDecimal;
import java.util.Map;
import java.util.Random;

/** Unit/property tests for the incremental engine, incl. acceptance scenarios. */
public final class MaterializedViewTest {

    private static int eventSeq;

    private static String eid() {
        return "evt-" + (eventSeq++);
    }

    private static BigDecimal money(String s) {
        // Mirrors the view's ingest rule: fixed point, scale 2, HALF_UP.
        return new BigDecimal(s).setScale(2, java.math.RoundingMode.HALF_UP);
    }

    private static Product product(String id, String category) {
        return new Product(id, category);
    }

    private static OrderLine line(String id, String pid, long qty, String amount) {
        return new OrderLine(id, pid, qty, money(amount));
    }

    public static int run() {
        TestKit t = new TestKit("MaterializedViewTest");
        testBasicUpsert(t);
        testOrderLineUpdate(t);
        testDeleteOrderLine(t);
        testDeleteMissing(t);
        testDuplicateEventId(t);
        testDuplicateEventIdDifferentPayload(t);
        testDimensionCategoryChange(t);
        testCategoryChangeWithoutLines(t);
        testLineBeforeProductThenMatch(t);
        testProductDeleteOrphansLines(t);
        testProductRecreateMovesLinesBack(t);
        testFixedPointMoney(t);
        testZeroBucketPruned(t);
        testDedupeScopeIsInstanceLifetime(t);
        testRandomizedVsFullRecompute(t, 42, 4000);
        testRandomizedVsFullRecompute(t, 7, 4000);
        return t.finish();
    }

    // --------------------------------------------------------------- tests

    private static void testBasicUpsert(TestKit t) {
        t.section("basic inserts on both sides");
        MaterializedView v = new MaterializedView();
        v.applyEvent(eid(), "product", "upsert", product("P1", "books"), null);
        v.applyEvent(eid(), "order_line", "upsert", null, line("L1", "P1", 2, "19.90"));
        v.applyEvent(eid(), "order_line", "upsert", null, line("L2", "P1", 1, "5.10"));

        Map<String, CategoryAggregate> a = v.aggregates();
        t.checkEq(1, a.size(), "one category");
        t.checkEq(3L, a.get("books").qty(), "books qty 3");
        t.checkEq(money("25.00"), a.get("books").amount(), "books amount 25.00");
        t.check(v.matchesFullRecompute(), "matches full recompute");
    }

    private static void testOrderLineUpdate(TestKit t) {
        t.section("order-line update (qty/amount/product change)");
        MaterializedView v = new MaterializedView();
        v.applyEvent(eid(), "product", "upsert", product("P1", "books"), null);
        v.applyEvent(eid(), "product", "upsert", product("P2", "music"), null);
        v.applyEvent(eid(), "order_line", "upsert", null, line("L1", "P1", 2, "19.90"));

        // update quantity + amount (amount is the line total, not unit price)
        v.applyEvent(eid(), "order_line", "upsert", null, line("L1", "P1", 3, "29.85"));
        t.checkEq(3L, v.aggregates().get("books").qty(), "qty updated to 3");
        t.checkEq(money("29.85"), v.aggregates().get("books").amount(), "amount updated to 29.85");

        // move line to another product/category
        v.applyEvent(eid(), "order_line", "upsert", null, line("L1", "P2", 1, "7.00"));
        t.check(v.aggregates().containsKey("books") == false, "books bucket pruned after move");
        t.checkEq(1L, v.aggregates().get("music").qty(), "music qty 1");
        t.checkEq(money("7.00"), v.aggregates().get("music").amount(), "music amount 7.00");
        t.check(v.matchesFullRecompute(), "matches full recompute");
    }

    private static void testDeleteOrderLine(TestKit t) {
        t.section("delete order line");
        MaterializedView v = new MaterializedView();
        v.applyEvent(eid(), "product", "upsert", product("P1", "books"), null);
        v.applyEvent(eid(), "order_line", "upsert", null, line("L1", "P1", 2, "19.90"));
        ApplyResult r = v.applyEvent(eid(), "order_line", "delete", null,
                line("L1", "P1", 0, "0"));
        t.check(r.changed() && !r.notFound(), "delete reports changed");
        t.check(v.aggregates().isEmpty(), "aggregates empty after deleting sole line");
        t.checkEq(0L, v.orderLines().size(), "line gone after delete");
        t.check(v.matchesFullRecompute(), "matches full recompute");
    }

    private static void testDeleteMissing(TestKit t) {
        t.section("delete non-existent rows is a safe no-op (still consumes eventId)");
        MaterializedView v = new MaterializedView();
        ApplyResult r1 = v.applyEvent(eid(), "order_line", "delete", null,
                line("NOPE", "", 0, "0"));
        ApplyResult r2 = v.applyEvent(eid(), "product", "delete", product("GHOST", "x"), null);
        t.check(!r1.changed() && r1.notFound(), "missing line delete flagged notFound");
        t.check(!r2.changed() && r2.notFound(), "missing product delete flagged notFound");
        t.check(v.aggregates().isEmpty(), "nothing aggregated");
        t.check(v.matchesFullRecompute(), "matches full recompute");

        // Replaying the notFound delete with the SAME id must be a duplicate,
        // even though the first application changed nothing.
        ApplyResult replay = v.applyEvent(r1.eventId(), "order_line", "delete", null,
                line("NOPE", "", 0, "0"));
        t.check(replay.duplicate(), "replayed delete is duplicate (id already consumed)");
    }

    private static void testDuplicateEventId(TestKit t) {
        t.section("duplicate business event (same eventId) is applied once");
        MaterializedView v = new MaterializedView();
        v.applyEvent(eid(), "product", "upsert", product("P1", "books"), null);
        v.applyEvent("dup-1", "order_line", "upsert", null, line("L1", "P1", 2, "19.90"));
        ApplyResult again = v.applyEvent("dup-1", "order_line", "upsert",
                null, line("L1", "P1", 2, "19.90"));
        t.check(again.duplicate(), "identical replay marked duplicate");
        t.checkEq(2L, v.aggregates().get("books").qty(), "qty still 2, not 4");
        t.checkEq(money("19.90"), v.aggregates().get("books").amount(), "amount counted once");
        t.checkEq(2L, v.appliedEvents(), "two applied events (product + line)");
        t.check(v.matchesFullRecompute(), "matches full recompute");
    }

    private static void testDuplicateEventIdDifferentPayload(TestKit t) {
        t.section("eventId dedupe ignores even a *different* payload under same id");
        MaterializedView v = new MaterializedView();
        v.applyEvent(eid(), "product", "upsert", product("P1", "books"), null);
        v.applyEvent("dup-2", "order_line", "upsert", null, line("L1", "P1", 2, "19.90"));
        // malicious/retried replay carrying changed numbers and even another id
        ApplyResult again = v.applyEvent("dup-2", "order_line", "upsert",
                null, line("L9", "P1", 99, "999.99"));
        t.check(again.duplicate(), "replay marked duplicate regardless of payload");
        t.checkEq(2L, v.aggregates().get("books").qty(), "qty untouched");
        t.checkEq(money("19.90"), v.aggregates().get("books").amount(), "amount untouched");
        t.checkEq(1L, v.orderLines().size(), "only L1 exists");
        t.check(v.matchesFullRecompute(), "matches full recompute");
    }

    private static void testDimensionCategoryChange(TestKit t) {
        t.section("dimension category change migrates joined lines (acceptance)");
        MaterializedView v = new MaterializedView();
        v.applyEvent(eid(), "product", "upsert", product("P1", "books"), null);
        v.applyEvent(eid(), "product", "upsert", product("P2", "books"), null);
        v.applyEvent(eid(), "order_line", "upsert", null, line("L1", "P1", 2, "10.00"));
        v.applyEvent(eid(), "order_line", "upsert", null, line("L2", "P2", 1, "5.00"));

        // Reclassify P1 books -> media. L1 must move, L2 must stay.
        ApplyResult r = v.applyEvent(eid(), "product", "upsert",
                product("P1", "media"), null);
        t.check(r.changed(), "category change reports changed");
        t.checkEq("books", r.categoryFrom(), "from books");
        t.checkEq("media", r.categoryTo(), "to media");
        t.checkEq(1, r.migratedOrderLines(), "one line migrated");

        CategoryAggregate books = v.aggregates().get("books");
        CategoryAggregate media = v.aggregates().get("media");
        t.checkEq(1L, books.qty(), "books keeps L2 qty 1");
        t.checkEq(money("5.00"), books.amount(), "books keeps L2 amount");
        t.checkEq(2L, media.qty(), "media gets L1 qty 2");
        t.checkEq(money("10.00"), media.amount(), "media gets L1 amount 10.00");        t.check(v.matchesFullRecompute(), "incremental == full recompute after recategorization");

        // And back again
        v.applyEvent(eid(), "product", "upsert", product("P1", "books"), null);
        t.checkEq(3L, v.aggregates().get("books").qty(), "all back under books");
        t.check(v.aggregates().containsKey("media") == false, "media bucket pruned");
        t.check(v.matchesFullRecompute(), "matches full recompute after moving back");
    }

    private static void testCategoryChangeWithoutLines(TestKit t) {
        t.section("category change with no joined lines migrates zero rows");
        MaterializedView v = new MaterializedView();
        v.applyEvent(eid(), "product", "upsert", product("P1", "books"), null);
        ApplyResult r = v.applyEvent(eid(), "product", "upsert", product("P1", "media"), null);
        t.check(r.changed() && r.migratedOrderLines() == 0, "changed but migrated 0");
        t.check(v.aggregates().isEmpty(), "no aggregates with no lines");
        t.check(v.matchesFullRecompute(), "matches full recompute");
    }

    private static void testLineBeforeProductThenMatch(TestKit t) {
        t.section("line arrives before product: unmatched, then joins on product insert");
        MaterializedView v = new MaterializedView();
        ApplyResult first = v.applyEvent(eid(), "order_line", "upsert",
                null, line("L1", "P1", 2, "19.90"));
        t.checkEq(1, first.unmatchedOrderLines(), "line initially unmatched");
        t.checkEq(2L, v.aggregates().get(MaterializedView.UNMATCHED).qty(),
                "unmatched bucket holds qty");

        ApplyResult matched = v.applyEvent(eid(), "product", "upsert",
                product("P1", "books"), null);
        t.checkEq(1, matched.migratedOrderLines(), "product insert migrated 1 waiting line");
        t.check(v.aggregates().containsKey(MaterializedView.UNMATCHED) == false,
                "unmatched bucket pruned");
        t.checkEq(2L, v.aggregates().get("books").qty(), "line now under books");
        t.check(v.matchesFullRecompute(), "matches full recompute");
    }

    private static void testProductDeleteOrphansLines(TestKit t) {
        t.section("deleting a product orphans its lines into the unmatched bucket");
        MaterializedView v = new MaterializedView();
        v.applyEvent(eid(), "product", "upsert", product("P1", "books"), null);
        v.applyEvent(eid(), "order_line", "upsert", null, line("L1", "P1", 2, "19.90"));
        ApplyResult r = v.applyEvent(eid(), "product", "delete", product("P1", "books"), null);
        t.checkEq(1, r.unmatchedOrderLines(), "one line orphaned");
        t.checkEq(2L, v.aggregates().get(MaterializedView.UNMATCHED).qty(),
                "orphaned qty preserved");
        t.checkEq(money("19.90"),
                v.aggregates().get(MaterializedView.UNMATCHED).amount(),
                "orphaned amount preserved");
        t.check(v.matchesFullRecompute(), "matches full recompute");

        // deleting the orphaned line removes the unmatched bucket
        v.applyEvent(eid(), "order_line", "delete", null, line("L1", "", 0, "0"));
        t.check(v.aggregates().isEmpty(), "all aggregates empty after orphan cleanup");
        t.check(v.matchesFullRecompute(), "matches full recompute");
    }

    private static void testProductRecreateMovesLinesBack(TestKit t) {
        t.section("re-adding a deleted product under a new category re-joins lines");
        MaterializedView v = new MaterializedView();
        v.applyEvent(eid(), "product", "upsert", product("P1", "books"), null);
        v.applyEvent(eid(), "order_line", "upsert", null, line("L1", "P1", 2, "19.90"));
        v.applyEvent(eid(), "product", "delete", product("P1", "books"), null);
        ApplyResult r = v.applyEvent(eid(), "product", "upsert",
                product("P1", "media"), null);
        t.checkEq(1, r.migratedOrderLines(), "1 line re-joined");
        t.checkEq(money("19.90"), v.aggregates().get("media").amount(),
                "line lands under new category");
        t.check(v.aggregates().containsKey(MaterializedView.UNMATCHED) == false,
                "no unmatched lines remain");
        t.check(v.matchesFullRecompute(), "matches full recompute");
    }

    private static void testFixedPointMoney(TestKit t) {
        t.section("money is exact fixed point (0.10 + 0.20 = 0.30, not 0.30000000004)");
        MaterializedView v = new MaterializedView();
        v.applyEvent(eid(), "product", "upsert", product("P1", "books"), null);
        v.applyEvent(eid(), "order_line", "upsert", null, line("L1", "P1", 1, "0.10"));
        v.applyEvent(eid(), "order_line", "upsert", null, line("L2", "P1", 1, "0.20"));
        CategoryAggregate agg = v.aggregates().get("books");
        t.checkEq(money("0.30"), agg.amount(), "exact 0.30");
        t.checkEq("0.30", agg.amount().toPlainString(), "plain string 0.30");
        t.checkEq(2, agg.amount().scale(), "scale is 2");

        // fractional-cent input is rounded HALF_UP once, deterministically
        v.applyEvent(eid(), "order_line", "upsert", null, line("L3", "P1", 1, "0.005"));
        t.checkEq(money("0.31"), v.aggregates().get("books").amount(),
                "0.005 rounds half-up to 0.01 on ingest");
        t.check(v.matchesFullRecompute(), "matches full recompute");
    }

    private static void testZeroBucketPruned(TestKit t) {
        t.section("categories whose totals fall to zero are pruned");
        MaterializedView v = new MaterializedView();
        v.applyEvent(eid(), "product", "upsert", product("P1", "books"), null);
        v.applyEvent(eid(), "order_line", "upsert", null, line("L1", "P1", 2, "19.90"));
        v.applyEvent(eid(), "order_line", "delete", null, line("L1", "", 0, "0"));
        t.check(v.aggregates().isEmpty(), "books bucket removed");
    }

    private static void testDedupeScopeIsInstanceLifetime(TestKit t) {
        t.section("dedupe scope = view instance lifetime (a fresh view reaccepts an id)");
        MaterializedView v1 = new MaterializedView();
        v1.applyEvent("scope-1", "product", "upsert", product("P1", "books"), null);
        t.check(v1.seenEventIds().contains("scope-1"), "id consumed in v1");

        MaterializedView v2 = new MaterializedView();
        ApplyResult accepted = v2.applyEvent("scope-1", "product", "upsert",
                product("P1", "music"), null);
        t.check(!accepted.duplicate(), "same id accepted by a separate view instance");
        t.checkEq("music", v2.products().get(0).category(), "v2 has its own state");
    }

    // ----------------------------------------------------- randomized check

    private static void testRandomizedVsFullRecompute(TestKit t, long seed, int steps) {
        t.section("randomized " + steps + "-step history vs full recompute (seed=" + seed + ")");
        Random rnd = new Random(seed);
        MaterializedView v = new MaterializedView();

        String[] products = {"P1", "P2", "P3", "P4"};
        String[] categories = {"books", "music", "games", "food"};
        String[] lines = {"L1", "L2", "L3", "L4", "L5"};

        for (int step = 0; step < steps; step++) {
            int kind = rnd.nextInt(10);
            String pid = products[rnd.nextInt(products.length)];
            String lid = lines[rnd.nextInt(lines.length)];
            // ~15% of events deliberately reuse a recent id to inject duplicates
            String id = (step > 10 && rnd.nextInt(100) < 15)
                    ? "rnd-" + rnd.nextInt(step)
                    : "rnd-" + step;
            try {
                switch (kind) {
                    case 0, 1, 2 -> v.applyEvent(id, "product", "upsert",
                            product(pid, categories[rnd.nextInt(categories.length)]), null);
                    case 3 -> v.applyEvent(id, "product", "delete", product(pid, ""), null);
                    case 4, 5, 6, 7, 8 -> {
                        long qty = rnd.nextInt(5);
                        String amount = BigDecimal.valueOf(rnd.nextInt(10000))
                                .movePointLeft(2).toPlainString();
                        v.applyEvent(id, "order_line", "upsert",
                                null, line(lid, pid, qty, amount));
                    }
                    default -> v.applyEvent(id, "order_line", "delete",
                            null, line(lid, "", 0, "0"));
                }
            } catch (IllegalArgumentException e) {
                throw new AssertionError("step " + step + " failed: " + e.getMessage(), e);
            }
            if (!v.matchesFullRecompute()) {
                t.check(false, "mismatch at step " + step
                        + "\n  incremental: " + v.aggregates()
                        + "\n  full:        " + v.fullRecompute());
                return;
            }
        }
        t.check(v.matchesFullRecompute(), "final state matches full recompute");
    }

    private MaterializedViewTest() {
    }
}
