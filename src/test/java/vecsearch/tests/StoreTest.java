package vecsearch.tests;

import vecsearch.core.Metric;
import vecsearch.core.SearchHit;
import vecsearch.core.SearchOutcome;
import vecsearch.core.TaggedVector;
import vecsearch.core.VectorStore;
import vecsearch.testutil.Assert;
import vecsearch.testutil.TestRunner;
import vecsearch.util.ApiException;

import java.util.List;
import java.util.Map;

/** VectorStore：维度校验、零向量策略、upsert/delete、过滤、精确检索与计数。 */
public final class StoreTest {

    private static TaggedVector tv(String id, float[] v, String... kv) {
        Map<String, String> f = new java.util.LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            f.put(kv[i], kv[i + 1]);
        }
        return new TaggedVector(id, v, f);
    }

    public static void register(TestRunner r) {
        r.test("dimension is locked on first insert; mismatches rejected 400", () -> {
            VectorStore s = new VectorStore(Metric.L2);
            s.upsert(tv("a", new float[]{1, 2, 3}));
            Assert.eq(s.dimension(), 3);
            ApiException ae = Assert.capture(() ->
                    s.upsert(tv("b", new float[]{1, 2})));
            Assert.eq(ae.status(), 400);
            Assert.contains(ae.getMessage(), "dimension mismatch");
            Assert.eq(s.size(), 1);
        });

        r.test("COSINE rejects zero vector on insert and on query", () -> {
            VectorStore s = new VectorStore(Metric.COSINE);
            ApiException e1 = Assert.capture(() ->
                    s.upsert(tv("z", new float[]{0, 0})));
            Assert.eq(e1.status(), 400);
            Assert.contains(e1.getMessage(), "zero vector");

            s.upsert(tv("a", new float[]{1, 0}));
            ApiException e2 = Assert.capture(() ->
                    s.exactSearch(new float[]{0, 0}, 1, null));
            Assert.eq(e2.status(), 400);
            Assert.contains(e2.getMessage(), "zero query");
        });

        r.test("L2 allows zero vectors and they participate in search", () -> {
            VectorStore s = new VectorStore(Metric.L2);
            s.upsert(tv("z", new float[]{0, 0}));
            SearchOutcome o = s.exactSearch(new float[]{0.1f, -0.1f}, 1, null);
            Assert.eq(o.hits().get(0).id(), "z");
            Assert.eq(o.exact(), true);
        });

        r.test("query dimension mismatch is 400", () -> {
            VectorStore s = new VectorStore(Metric.L2);
            s.upsert(tv("a", new float[]{1, 2, 3}));
            ApiException e = Assert.capture(() ->
                    s.exactSearch(new float[]{1, 2}, 1, null));
            Assert.eq(e.status(), 400);
        });

        r.test("upsert replaces existing id; delete removes it", () -> {
            VectorStore s = new VectorStore(Metric.L2);
            Assert.eq(s.upsert(tv("a", new float[]{1, 1})), true);
            Assert.eq(s.upsert(tv("a", new float[]{2, 2})), false);
            Assert.eq(s.size(), 1);
            Assert.eq(s.delete("a"), true);
            Assert.eq(s.delete("a"), false);
            Assert.eq(s.size(), 0);
        });

        r.test("deleted id never appears in exact or filtered search results", () -> {
            VectorStore s = new VectorStore(Metric.L2);
            for (int i = 0; i < 20; i++) {
                s.upsert(tv("v" + i, new float[]{i, i}, "bucket", i % 2 == 0 ? "even" : "odd"));
            }
            s.delete("v5");
            s.delete("v6");
            SearchOutcome all = s.exactSearch(new float[]{5, 5}, 20, null);
            for (SearchHit h : all.hits()) {
                Assert.isTrue(!h.id().equals("v5") && !h.id().equals("v6"),
                        "deleted id returned: " + h.id());
            }
            SearchOutcome evens = s.exactSearch(new float[]{5, 5}, 20, Map.of("bucket", "even"));
            for (SearchHit h : evens.hits()) {
                Assert.eq(h.filter().get("bucket"), "even");
            }
            Assert.isTrue(evens.hits().stream().noneMatch(h -> h.id().equals("v6")));
        });

        r.test("filter is AND over all requested key-values", () -> {
            VectorStore s = new VectorStore(Metric.L2);
            s.upsert(tv("a", new float[]{0, 0}, "x", "1", "y", "p"));
            s.upsert(tv("b", new float[]{0, 0}, "x", "1", "y", "q"));
            s.upsert(tv("c", new float[]{0, 0}, "x", "2", "y", "p"));
            SearchOutcome o = s.exactSearch(new float[]{0, 0}, 10, Map.of("x", "1", "y", "p"));
            Assert.eq(o.hits().size(), 1);
            Assert.eq(o.hits().get(0).id(), "a");
        });

        r.test("exact search returns true k-nearest sorted ascending", () -> {
            VectorStore s = new VectorStore(Metric.L2);
            s.upsert(tv("p0", new float[]{0, 0}));
            s.upsert(tv("p1", new float[]{1, 0}));
            s.upsert(tv("p2", new float[]{2, 0}));
            s.upsert(tv("p3", new float[]{10, 0}));
            SearchOutcome o = s.exactSearch(new float[]{1.2f, 0}, 2, null);
            List<String> ids = o.hits().stream().map(SearchHit::id).toList();
            Assert.eq(ids, List.of("p1", "p2"));
            Assert.approx(o.hits().get(0).distance(), 0.2, 1e-6);
        });

        r.test("exact search counts one distance computation per surviving vector", () -> {
            VectorStore s = new VectorStore(Metric.L2);
            for (int i = 0; i < 10; i++) {
                s.upsert(tv("v" + i, new float[]{i, 0}, "keep", i < 6 ? "y" : "n"));
            }
            SearchOutcome unfiltered = s.exactSearch(new float[]{0, 0}, 3, null);
            Assert.eq(unfiltered.computations(), 10L);
            SearchOutcome filtered = s.exactSearch(new float[]{0, 0}, 3, Map.of("keep", "y"));
            Assert.eq(filtered.computations(), 6L);
        });

        r.test("batch upsert is all-or-nothing on validation failure", () -> {
            VectorStore s = new VectorStore(Metric.L2);
            s.upsert(tv("seed", new float[]{1, 1}));
            List<TaggedVector> bad = List.of(
                    tv("ok", new float[]{2, 2}),
                    tv("wrongdim", new float[]{1, 2, 3}));
            ApiException e = Assert.capture(() -> s.upsertAll(bad));
            Assert.eq(e.status(), 400);
            Assert.eq(s.size(), 1);
            Assert.isTrue(s.get("ok") == null, "partial batch write leaked");
        });

        r.test("empty store search returns empty hits, no error", () -> {
            VectorStore s = new VectorStore(Metric.L2);
            SearchOutcome o = s.exactSearch(new float[]{1, 2, 3}, 5, null);
            Assert.eq(o.hits().size(), 0);
            Assert.eq(o.computations(), 0L);
        });
    }

    private StoreTest() {
    }
}
