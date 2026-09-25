package neardup;

import neardup.core.Jaccard;

import java.util.Set;

import static neardup.TestRunner.approx;
import static neardup.TestRunner.check;

final class JaccardTest {

    static void run() {
        TestRunner.group("jaccard");

        check(Jaccard.similarity(Set.of(), Set.of()) == 0.0,
                "two empty sets -> 0 (empty docs never match)");
        check(Jaccard.similarity(Set.of(), Set.of(1L)) == 0.0, "empty vs non-empty -> 0");
        check(Jaccard.similarity(Set.of(1L, 2L), Set.of(1L, 2L)) == 1.0, "identical -> 1");
        check(Jaccard.similarity(Set.of(1L), Set.of(2L)) == 0.0, "disjoint -> 0");

        // 1 intersection / 5 union = 0.2 — the short-document counter-example ratio.
        approx(Jaccard.similarity(Set.of(1L), Set.of(1L, 2L, 3L, 4L, 5L)), 0.2, 1e-12,
                "|1|/|5| = 0.2");
        // 6/10 = 0.6 — transitive-chain edge ratio.
        approx(Jaccard.similarity(
                        Set.of(1L, 2L, 3L, 4L, 5L, 6L, 7L, 8L),
                        Set.of(3L, 4L, 5L, 6L, 7L, 8L, 9L, 10L)), 0.6, 1e-12,
                "8-sets sharing 6 -> 0.6");
        // 4/12 = 0.3333 — chain endpoints.
        approx(Jaccard.similarity(
                        Set.of(1L, 2L, 3L, 4L, 5L, 6L, 7L, 8L),
                        Set.of(5L, 6L, 7L, 8L, 9L, 10L, 11L, 12L)),
                4.0 / 12.0, 1e-12, "8-sets sharing 4 -> 1/3");

        // Symmetric
        Set<Long> a = Set.of(1L, 2L, 3L);
        Set<Long> b = Set.of(2L, 3L, 4L);
        check(Jaccard.similarity(a, b) == Jaccard.similarity(b, a), "symmetric");
    }
}
