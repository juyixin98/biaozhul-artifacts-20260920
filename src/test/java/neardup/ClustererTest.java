package neardup;

import neardup.core.Clusterer;
import neardup.corpus.SyntheticCorpus;

import java.util.HashSet;
import java.util.List;
import java.util.Set;

import static neardup.TestRunner.approx;
import static neardup.TestRunner.check;

final class ClustererTest {

    static void run() {
        TestRunner.group("clusterer");

        List<SyntheticCorpus.Doc> meta = SyntheticCorpus.corpus();
        List<String> texts = SyntheticCorpus.texts();
        int n = texts.size();

        // index lookup helper
        java.util.Map<String, Integer> idx = new java.util.HashMap<>();
        for (int i = 0; i < meta.size(); i++) {
            idx.put(meta.get(i).id(), i);
        }

        Clusterer.Result r = new Clusterer(0.6).cluster(texts);
        Clusterer.Stats st = r.stats();

        // ---- Basic accounting ------------------------------------------------
        check(st.documents() == n, "stats documents = corpus size");
        check(st.emptyShingleDocuments() >= 2,
                "at least two empty-shingle documents (s1,s2)",
                "count=" + st.emptyShingleDocuments());
        check(st.bruteForcePairs() == (long) n * (n - 1) / 2, "bruteForcePairs = n(n-1)/2");

        // ---- Ground truth edge count by construction -------------------------
        // The 7-doc ALPHA component contains 16 threshold edges (dense rewrites:
        // e.g. a1 reaches a4/a5 directly too), CHAIN has its 2 adjacent edges,
        // BETA 1, ZH 1 -> 20 true edges at threshold 0.6. Importantly the
        // ALPHA size-7 cluster has 7*6/2 = 21 pairs total, so even there not
        // every pair is an edge (a1-a7 etc. remain below threshold).
        check(st.trueEdges() == 20, "20 true edges by construction",
                "observed " + st.trueEdges());

        // ---- Candidate recall: all 10 true edges recalled --------------------
        check(st.trueEdgesMissed() == 0, "zero true edges missed by LSH recall",
                "missed=" + st.trueEdgesMissed());
        approx(st.candidateRecall(), 1.0, 1e-12, "candidate recall = 1.0 on corpus");

        // ---- False positives: candidates that fail exact verification --------
        check(st.candidatePairs() >= 10, "at least one candidate per true edge",
                "candidates=" + st.candidatePairs());
        check(st.candidateFalsePositives() > 0,
                "LSH emits false-positive candidates (recheck stage is exercised)",
                "falsePositives=" + st.candidateFalsePositives());
        approx(st.falsePositiveShareOfCandidates(),
                (double) st.candidateFalsePositives() / st.candidatePairs(), 1e-12,
                "reported false-positive share consistent with counts");
        approx(st.candidatePrecision(),
                (double) st.candidatesAccepted() / st.candidatePairs(), 1e-12,
                "reported candidate precision consistent with counts");

        // Every false-positive candidate really is below the exact threshold,
        // and every accepted one really is >= threshold.
        boolean classificationConsistent = true;
        for (Clusterer.Candidate c : r.candidates()) {
            if (c.accepted() != (c.exactJaccard() >= 0.6)) {
                classificationConsistent = false;
            }
        }
        check(classificationConsistent, "candidate acceptance matches exact Jaccard");

        // ---- Cluster membership (connected components) -----------------------
        // Four multi-document clusters expected: ALPHA(7), CHAIN(3), BETA(2), ZH(2).
        List<Integer> sizes = r.clusters().stream().map(c -> c.members().size()).sorted().toList();
        check(sizes.equals(List.of(2, 2, 3, 7)), "cluster sizes are {7,3,2,2}",
                "sizes=" + sizes);

        Set<Set<Integer>> compSets = new HashSet<>();
        for (Clusterer.Cluster c : r.clusters()) {
            compSets.add(new HashSet<>(c.members()));
        }
        Set<Integer> alpha = new HashSet<>();
        for (String id : List.of("a1", "a2", "a3", "a4", "a5", "a6", "a7")) {
            alpha.add(idx.get(id));
        }
        check(compSets.contains(alpha), "ALPHA cluster = {a1..a7}, a8 excluded");

        Set<Integer> chain = new HashSet<>();
        for (String id : List.of("c1", "c2", "c3")) {
            chain.add(idx.get(id));
        }
        check(compSets.contains(chain), "CHAIN cluster = {c1,c2,c3} via transitivity");

        // ---- Transitive-chain semantics (the headline acceptance case) -------
        Clusterer.Cluster chainCluster = null;
        for (Clusterer.Cluster c : r.clusters()) {
            if (new HashSet<>(c.members()).equals(chain)) {
                chainCluster = c;
            }
        }
        check(chainCluster != null, "found the chain cluster");
        check(chainCluster.internalEdges().size() == 2, "chain has exactly 2 edges (adjacent)");
        check(chainCluster.belowThresholdPairs().size() == 1,
                "chain has exactly 1 below-threshold pair (the endpoints)");
        Clusterer.TransitivePair endpoints = chainCluster.belowThresholdPairs().get(0);
        check(Set.of(endpoints.i(), endpoints.j()).equals(Set.of(idx.get("c1"), idx.get("c3"))),
                "below-threshold pair is precisely (c1,c3)");
        approx(endpoints.exactJaccard(), 4.0 / 12.0, 1e-12,
                "c1 vs c3 exact Jaccard = 1/3 (< 0.5) yet same cluster");

        // ALPHA distant endpoints also below threshold while clustered.
        int a1 = idx.get("a1");
        int a7 = idx.get("a7");
        Clusterer.TransitivePair alphaEnds = null;
        for (Clusterer.Cluster c : r.clusters()) {
            if (c.members().contains(a1)) {
                for (Clusterer.TransitivePair tp : c.belowThresholdPairs()) {
                    if (Set.of(tp.i(), tp.j()).equals(Set.of(a1, a7))) {
                        alphaEnds = tp;
                    }
                }
            }
        }
        check(alphaEnds != null, "a1 vs a7 listed as below-threshold transitive pair");
        approx(alphaEnds.exactJaccard(), 8.0 / 18.0, 1e-12,
                "a1 vs a7 = 8/18 = 0.444 (< 0.5) yet same cluster via the rewrite chain");

        // ---- Short-document counter-examples ---------------------------------
        int s1 = idx.get("s1");
        int s2 = idx.get("s2");
        boolean s1s2Together = r.clusters().stream()
                .anyMatch(c -> c.members().contains(s1) && c.members().contains(s2));
        check(!s1s2Together,
                "identical 2-token texts s1,s2 are NOT clustered (0 shingles, fixed rule)");
        boolean s1s2Candidate = r.candidates().stream()
                .anyMatch(c -> Set.of(c.i(), c.j()).equals(Set.of(s1, s2)));
        check(!s1s2Candidate, "s1,s2 are not even LSH candidates");

        int s3 = idx.get("s3");
        int s4 = idx.get("s4");
        boolean s3s4Together = r.clusters().stream()
                .anyMatch(c -> c.members().contains(s3) && c.members().contains(s4));
        check(!s3s4Together,
                "s3,s4 sharing the tiny doc's only shingle (Jaccard 0.2) are NOT clustered");

        // All four short documents end up as singletons.
        Set<Integer> singletonIdx = new HashSet<>();
        for (Clusterer.Cluster c : r.singletonClusters()) {
            singletonIdx.add(c.members().get(0));
        }
        check(singletonIdx.containsAll(Set.of(s1, s2, s3, s4, idx.get("a8"),
                        idx.get("x1"), idx.get("x2"))),
                "s1,s2,s3,s4,a8,x1,x2 all singleton");

        // ---- Threshold boundary behavior -------------------------------------
        Clusterer.Result strict = new Clusterer(0.8).cluster(texts);
        // At 0.8 only identical/near-identical pairs survive: a1-a2 (1.0) and
        // a1-a3 (12/14=0.857). Chain edges at 0.6 disappear -> c1,c2,c3 separate.
        boolean chainSplit = strict.clusters().stream()
                .noneMatch(c -> c.members().containsAll(Set.of(
                        idx.get("c1"), idx.get("c2"), idx.get("c3"))));
        check(chainSplit, "at threshold 0.8 the 0.6-chain does not merge");

        // ---- Custom arbitrary texts work -------------------------------------
        Clusterer.Result custom = new Clusterer(0.6).cluster(List.of(
                "alpha bravo charlie delta. echo foxtrot golf hotel.",
                "alpha bravo charlie delta. echo foxtrot golf hotel.",
                "completely different content appears here today."));
        check(custom.stats().trueEdges() == 1, "custom input: one identical pair");
        check(custom.clusters().size() == 1 && custom.clusters().get(0).members().size() == 2,
                "custom input: exactly one size-2 cluster");
    }
}
