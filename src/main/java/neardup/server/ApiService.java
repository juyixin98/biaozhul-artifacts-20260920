package neardup.server;

import neardup.corpus.SyntheticCorpus;
import neardup.core.Clusterer;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Turns pipeline results into plain maps that {@link Json} can serialize. */
public final class ApiService {

    private ApiService() {
    }

    public static Map<String, Object> health() {
        return Map.of("status", "ok", "service", "near-duplicate-clustering",
                "minhashSeed", neardup.core.MinHash.SEED,
                "minhashNumHashes", neardup.core.MinHash.DEFAULT_NUM_HASHES,
                "lshBands", neardup.core.Lsh.DEFAULT_BANDS,
                "lshRows", neardup.core.Lsh.DEFAULT_ROWS,
                "shingleK", neardup.core.Shingler.DEFAULT_K);
    }

    public static Map<String, Object> corpus() {
        List<Object> docs = new ArrayList<>();
        int idx = 0;
        for (SyntheticCorpus.Doc d : SyntheticCorpus.corpus()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("index", idx++);
            m.put("id", d.id());
            m.put("label", d.label());
            m.put("text", d.text());
            docs.add(m);
        }
        return Map.of("corpus", docs, "size", docs.size());
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> cluster(Object body) {
        double threshold = 0.6;
        List<String> texts;

        if (body instanceof Map<?, ?> map) {
            Object th = map.get("threshold");
            if (th instanceof Number n) {
                threshold = n.doubleValue();
            }
            Object ts = map.get("texts");
            if (ts instanceof List<?> list) {
                texts = new ArrayList<>();
                for (Object o : list) {
                    if (o instanceof Map<?, ?>) {
                        @SuppressWarnings("unchecked")
                        Map<String, Object> doc = (Map<String, Object>) o;
                        Object t = doc.get("text");
                        texts.add(t == null ? "" : t.toString());
                    } else {
                        texts.add(String.valueOf(o));
                    }
                }
            } else {
                // No texts field -> built-in synthetic corpus
                texts = SyntheticCorpus.texts();
            }
        } else {
            texts = SyntheticCorpus.texts();
        }

        return clusterResult(texts, threshold);
    }

    public static Map<String, Object> clusterResult(List<String> texts, double threshold) {
        Clusterer clusterer = new Clusterer(threshold);
        Clusterer.Result r = clusterer.cluster(texts);
        List<SyntheticCorpus.Doc> meta = SyntheticCorpus.corpus();
        boolean metaMatches = meta.size() == texts.size();

        Map<String, Object> root = new LinkedHashMap<>();
        root.put("threshold", threshold);
        root.put("note", "A cluster is a connected component of exact-Jaccard edges; "
                + "not every pair inside a cluster is >= threshold. See "
                + "clusters[].belowThresholdPairs for the transitive pairs.");

        // Stats
        Clusterer.Stats st = r.stats();
        Map<String, Object> stats = new LinkedHashMap<>();
        stats.put("documents", st.documents());
        stats.put("emptyShingleDocuments", st.emptyShingleDocuments());
        stats.put("bruteForcePairs", st.bruteForcePairs());
        stats.put("trueEdges", st.trueEdges());
        stats.put("candidatePairs", st.candidatePairs());
        stats.put("candidatesAccepted", st.candidatesAccepted());
        stats.put("candidateFalsePositives", st.candidateFalsePositives());
        stats.put("trueEdgesRecalled", st.trueEdgesRecalled());
        stats.put("trueEdgesMissed", st.trueEdgesMissed());
        stats.put("candidateRecall", round(st.candidateRecall()));
        stats.put("candidatePrecision", round(st.candidatePrecision()));
        stats.put("falsePositiveShareOfCandidates", round(st.falsePositiveShareOfCandidates()));
        root.put("stats", stats);

        // Clusters
        root.put("clusters", serializeClusters(r.clusters(), texts, meta, metaMatches));
        root.put("singletonCount", r.singletonClusters().size());
        root.put("singletons", serializeSingletons(r.singletonClusters(), texts, meta, metaMatches));

        // All candidates with exact verification
        List<Object> cands = new ArrayList<>();
        for (Clusterer.Candidate c : r.candidates()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("i", c.i());
            m.put("j", c.j());
            if (metaMatches) {
                m.put("iId", meta.get(c.i()).id());
                m.put("jId", meta.get(c.j()).id());
            }
            m.put("exactJaccard", round(c.exactJaccard()));
            m.put("minhashEstimate", round(c.minhashEstimate()));
            m.put("bandHits", c.bandHits());
            m.put("accepted", c.accepted());
            if (!c.accepted()) {
                m.put("why", "LSH candidate but exact Jaccard < threshold (false positive)");
            }
            cands.add(m);
        }
        root.put("candidates", cands);

        List<Object> missed = new ArrayList<>();
        for (int[] p : r.missedEdges()) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("i", p[0]);
            m.put("j", p[1]);
            if (metaMatches) {
                m.put("iId", meta.get(p[0]).id());
                m.put("jId", meta.get(p[1]).id());
            }
            missed.add(m);
        }
        root.put("missedTrueEdges", missed);

        return root;
    }

    private static List<Object> serializeClusters(List<Clusterer.Cluster> clusters,
                                                  List<String> texts,
                                                  List<SyntheticCorpus.Doc> meta,
                                                  boolean metaMatches) {
        List<Object> out = new ArrayList<>();
        int clusterId = 0;
        for (Clusterer.Cluster c : clusters) {
            Map<String, Object> m = new LinkedHashMap<>();
            m.put("clusterId", clusterId++);
            m.put("size", c.members().size());
            List<Object> members = new ArrayList<>();
            for (int idx : c.members()) {
                Map<String, Object> mm = new LinkedHashMap<>();
                mm.put("index", idx);
                if (metaMatches) {
                    mm.put("id", meta.get(idx).id());
                    mm.put("label", meta.get(idx).label());
                }
                members.add(mm);
            }
            m.put("members", members);

            List<Object> edges = new ArrayList<>();
            for (Clusterer.Edge e : c.internalEdges()) {
                Map<String, Object> em = new LinkedHashMap<>();
                em.put("i", e.i());
                em.put("j", e.j());
                if (metaMatches) {
                    em.put("iId", meta.get(e.i()).id());
                    em.put("jId", meta.get(e.j()).id());
                }
                em.put("exactJaccard", round(e.exactJaccard()));
                em.put("minhashEstimate", round(e.minhashEstimate()));
                em.put("bandHits", e.bandHits());
                edges.add(em);
            }
            m.put("edges", edges);

            List<Object> below = new ArrayList<>();
            for (Clusterer.TransitivePair tp : c.belowThresholdPairs()) {
                Map<String, Object> tm = new LinkedHashMap<>();
                tm.put("i", tp.i());
                tm.put("j", tp.j());
                if (metaMatches) {
                    tm.put("iId", meta.get(tp.i()).id());
                    tm.put("jId", meta.get(tp.j()).id());
                }
                tm.put("exactJaccard", round(tp.exactJaccard()));
                tm.put("note", "same cluster via transitive chain, but this pair itself is below threshold");
                below.add(tm);
            }
            m.put("belowThresholdPairs", below);
            out.add(m);
        }
        return out;
    }

    private static List<Object> serializeSingletons(List<Clusterer.Cluster> singletons,
                                                    List<String> texts,
                                                    List<SyntheticCorpus.Doc> meta,
                                                    boolean metaMatches) {
        List<Object> out = new ArrayList<>();
        for (Clusterer.Cluster c : singletons) {
            int idx = c.members().get(0);
            Map<String, Object> mm = new LinkedHashMap<>();
            mm.put("index", idx);
            if (metaMatches) {
                mm.put("id", meta.get(idx).id());
                mm.put("label", meta.get(idx).label());
            }
            out.add(mm);
        }
        return out;
    }

    private static double round(double v) {
        return Math.round(v * 100000.0) / 100000.0;
    }
}
