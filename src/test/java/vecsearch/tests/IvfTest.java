package vecsearch.tests;

import vecsearch.core.DistanceMeter;
import vecsearch.core.Metric;
import vecsearch.core.SearchHit;
import vecsearch.core.TaggedVector;
import vecsearch.core.VectorStore;
import vecsearch.eval.DataGenerator;
import vecsearch.index.IndexOptions;
import vecsearch.index.IvfIndex;
import vecsearch.index.StoreView;
import vecsearch.testutil.Assert;
import vecsearch.testutil.TestRunner;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/** IVF 近似索引：训练、预算-召回、距离计数、删除/过滤安全、增量写入、余弦一致性。 */
public final class IvfTest {

    private static List<TaggedVector> toVectors(DataGenerator.Dataset ds) {
        List<TaggedVector> vs = new ArrayList<>(ds.size());
        for (DataGenerator.DataPoint p : ds.points()) {
            vs.add(new TaggedVector(p.id(), p.vector(), p.tags()));
        }
        return vs;
    }

    private static StoreView viewOf(List<TaggedVector> vs, Metric m) {
        return new StoreView() {
            @Override
            public List<Item> items() {
                List<Item> out = new ArrayList<>(vs.size());
                for (TaggedVector v : vs) {
                    out.add(new Item(v.id(), v.vector(), v.filter()));
                }
                return out;
            }

            @Override
            public int dimension() {
                return vs.isEmpty() ? -1 : vs.get(0).vector().length;
            }

            @Override
            public Metric metric() {
                return m;
            }
        };
    }

    private static List<String> ids(List<SearchHit> hits) {
        return hits.stream().map(SearchHit::id).toList();
    }

    public static void register(TestRunner r) {
        DataGenerator.Dataset small =
                DataGenerator.generate(8, 4, 50, 6, 12, 5.0, 0.4, 20260923L);
        List<TaggedVector> smallVecs = toVectors(small);

        r.test("IVF nprobe=nlist is exhaustive and exactly matches brute force (L2)", () -> {
            VectorStore store = new VectorStore(Metric.L2);
            store.upsertAll(smallVecs);
            store.rebuildIndex(new IndexOptions(8, 25, 42));
            for (float[] q : small.queries()) {
                var exact = store.exactSearch(q, 10, null);
                var approx = store.approxSearch(q, 10, null, 8);
                Assert.eq(ids(approx.hits()), ids(exact.hits()),
                        "full-probe result mismatch for query");
                for (int i = 0; i < exact.hits().size(); i++) {
                    Assert.approx(approx.hits().get(i).distance(),
                            exact.hits().get(i).distance(), 1e-4, "distance mismatch");
                }
            }
        });

        r.test("IVF distances equal real L2 distances (sqrt applied)", () -> {
            VectorStore store = new VectorStore(Metric.L2);
            store.upsertAll(smallVecs);
            var approx = store.approxSearch(small.queries().get(0), 5, null, 2);
            var exact = store.exactSearch(small.queries().get(0), smallVecs.size(), null);
            var byId = new java.util.HashMap<String, Float>();
            exact.hits().forEach(h -> byId.put(h.id(), h.distance()));
            for (SearchHit h : approx.hits()) {
                Assert.approx(h.distance(), byId.get(h.id()), 1e-4);
            }
        });

        r.test("distance computations decrease monotonically as nprobe shrinks", () -> {
            VectorStore store = new VectorStore(Metric.L2);
            store.upsertAll(smallVecs);
            float[] q = small.queries().get(0);
            long cFull = store.approxSearch(q, 10, null, 8).computations();
            long cHalf = store.approxSearch(q, 10, null, 4).computations();
            long cOne = store.approxSearch(q, 10, null, 1).computations();
            Assert.isTrue(cOne < cHalf, cOne + " < " + cHalf);
            Assert.isTrue(cHalf < cFull, cHalf + " < " + cFull);
            // 全探测：nlist 个簇心 + 全部点
            Assert.eq(cFull, (long) 8 + smallVecs.size());
        });

        r.test("deleted vectors are physically absent from IVF postings", () -> {
            VectorStore store = new VectorStore(Metric.L2);
            store.upsertAll(smallVecs);
            store.approxSearch(small.queries().get(0), 1, null, 1); // 触发懒构建
            String victim = smallVecs.get(10).id();
            Assert.eq(store.delete(victim), true);
            for (int probe : new int[]{1, 4, 8}) {
                for (float[] q : small.queries()) {
                    var o = store.approxSearch(q, 20, null, probe);
                    for (SearchHit h : o.hits()) {
                        Assert.isTrue(!h.id().equals(victim),
                                "deleted id leaked at nprobe=" + probe);
                    }
                }
            }
        });

        r.test("incremental upsert after build inserts new id and it is searchable", () -> {
            IvfIndex idx = new IvfIndex(Metric.L2);
            idx.rebuild(viewOf(smallVecs.subList(0, 100), Metric.L2),
                    new IndexOptions(4, 20, 7));
            TaggedVector newbie = new TaggedVector("brand-new", new float[]{0, 0, 0, 0, 0, 0, 0, 0},
                    Map.of("kind", "core"));
            idx.upsert(newbie);
            Assert.eq(idx.indexedCount(), 101);
            DistanceMeter meter = new DistanceMeter(Metric.L2);
            float[] q = new float[]{0, 0, 0, 0, 0, 0, 0, 0};
            var hits = idx.search(q, 5, null, idx.clusterCount(), meter);
            Assert.isTrue(hits.stream().anyMatch(h -> h.id().equals("brand-new")),
                    "new id not found after incremental upsert");
        });

        r.test("upsert replacement does not duplicate id in postings", () -> {
            IvfIndex idx = new IvfIndex(Metric.L2);
            idx.rebuild(viewOf(smallVecs, Metric.L2), new IndexOptions(4, 20, 7));
            int before = idx.indexedCount();
            var existing = smallVecs.get(0);
            idx.upsert(new TaggedVector(existing.id(), existing.vector(), existing.filter()));
            Assert.eq(idx.indexedCount(), before);
            DistanceMeter meter = new DistanceMeter(Metric.L2);
            var hits = idx.search(smallVecs.get(0).vector(), smallVecs.size(),
                    null, idx.clusterCount(), meter);
            long count = hits.stream().filter(h -> h.id().equals(existing.id())).count();
            Assert.eq(count, 1L);
        });

        r.test("filtered ANN never returns vectors failing the filter", () -> {
            VectorStore store = new VectorStore(Metric.L2);
            store.upsertAll(smallVecs);
            for (int probe : new int[]{1, 2, 4, 8}) {
                for (float[] q : small.queries()) {
                    var o = store.approxSearch(q, 15, Map.of("group", "A"), probe);
                    for (SearchHit h : o.hits()) {
                        Assert.eq(h.filter().get("group"), "A",
                                "filter violated at nprobe=" + probe);
                    }
                }
            }
        });

        r.test("COSINE: full-probe ANN matches brute force and zero vectors stay rejected", () -> {
            DataGenerator.Dataset cos =
                    DataGenerator.generate(6, 3, 60, 4, 10, 3.0, 0.3, 20260923L);
            VectorStore store = new VectorStore(Metric.COSINE);
            store.upsertAll(toVectors(cos));
            store.rebuildIndex(new IndexOptions(6, 30, 42));
            for (float[] q : cos.queries()) {
                var exact = store.exactSearch(q, 10, null);
                var approx = store.approxSearch(q, 10, null, 6);
                Assert.eq(ids(approx.hits()), ids(exact.hits()), "cosine ids mismatch");
                for (int i = 0; i < exact.hits().size(); i++) {
                    Assert.approx(approx.hits().get(i).distance(),
                            exact.hits().get(i).distance(), 1e-4, "cosine distance");
                    Assert.isTrue(approx.hits().get(i).distance() >= -1e-6f
                            && approx.hits().get(i).distance() <= 2.0 + 1e-6);
                }
            }
            Assert.throws_(Exception.class,
                    () -> store.approxSearch(new float[6], 5, null, 1),
                    "zero query must be rejected under cosine");
        });

        r.test("training is deterministic for a fixed seed", () -> {
            IvfIndex a = new IvfIndex(Metric.L2);
            IvfIndex b = new IvfIndex(Metric.L2);
            IndexOptions opts = new IndexOptions(6, 25, 99);
            a.rebuild(viewOf(smallVecs, Metric.L2), opts);
            b.rebuild(viewOf(smallVecs, Metric.L2), opts);
            Assert.eq(a.describe().get("postingSizes"), b.describe().get("postingSizes"));
        });

        r.test("empty index search returns empty without error", () -> {
            IvfIndex idx = new IvfIndex(Metric.L2);
            idx.rebuild(viewOf(List.of(), Metric.L2), new IndexOptions(4, 10, 1));
            Assert.eq(idx.clusterCount(), 0);
            DistanceMeter meter = new DistanceMeter(Metric.L2);
            Assert.eq(idx.search(new float[]{1, 2}, 3, null, 4, meter).size(), 0);
        });
    }

    private IvfTest() {
    }
}
