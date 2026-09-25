package neardup.corpus;

import java.util.ArrayList;
import java.util.List;

/**
 * Self-contained, fully SYNTHETIC corpus. No external data, no models.
 *
 * Construction trick: each "unit" of content is one sentence with exactly
 * k=3 tokens ("uXX yy zz"), and every unit's three tokens are globally unique.
 * Shingles never cross sentence boundaries, so each unit contributes exactly
 * one unique shingle. Document shingle sets can therefore be controlled
 * precisely by the set of unit ids they contain, which lets us prove the
 * acceptance cases by construction:
 *
 *  - ALPHA near-duplicate cluster (7 docs): reorders (same set), edits, and
 *    word-level rewrites around a 13-unit core. Every variant is joined to the
 *    core by an exact-Jaccard edge >= 0.6.
 *  - CHAIN: c1-c2-c3, adjacent Jaccard 0.6/0.6 but c1 vs c3 = 1/3 (< 0.5).
 *    Must become ONE cluster while the endpoint pair stays below threshold.
 *  - BETA cluster (2 docs, Jaccard 5/7 ≈ 0.714).
 *  - ZH cluster: Chinese near-duplicates, per-character CJK tokenization.
 *  - SHORT counter-examples: two identical 2-token sentences (empty shingle
 *    set -> not merged despite identical text), and two short documents
 *    sharing their only shingle (Jaccard 1/5 = 0.2 -> not merged).
 *  - Two unrelated singletons.
 */
public final class SyntheticCorpus {

    public record Doc(String id, String label, String text) {}

    private SyntheticCorpus() {
    }

    /** Renders one globally-unique 3-token sentence for unit number n (1-based). */
    private static String unit(int n) {
        // Three tokens, unique across the whole corpus:
        //   token1 "uXX..." never repeats; token2 carries n; token3 too.
        return "unit" + String.format("%02d", n) + " word" + n + " tail" + n + ".";
    }

    /** A document assembled from the given unit numbers, in order. */
    private static String doc(int... units) {
        StringBuilder sb = new StringBuilder();
        for (int u : units) {
            if (!sb.isEmpty()) {
                sb.append(' ');
            }
            sb.append(unit(u));
        }
        return sb.toString();
    }

    /** Same units in a deterministic different line order (lines are segment breaks). */
    private static String reordered(int... units) {
        StringBuilder sb = new StringBuilder();
        for (int idx = units.length - 1; idx >= 0; idx--) {
            sb.append(unit(units[idx]));
            sb.append('\n');
        }
        return sb.toString();
    }

    public static List<Doc> corpus() {
        List<Doc> d = new ArrayList<>();

        // ---- ALPHA: core units 1..13 -------------------------------------
        int[] alphaCore = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13};
        d.add(new Doc("a1", "alpha original", doc(alphaCore)));
        d.add(new Doc("a2", "alpha exact reorder (same shingle set)", reordered(alphaCore)));
        d.add(new Doc("a3", "alpha small edit (12/14)",
                doc(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14))); // 13->14
        d.add(new Doc("a4", "alpha medium edit (11/15)",
                doc(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 14, 15)));
        d.add(new Doc("a5", "alpha rewrite (10/16)",
                doc(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 14, 15, 16)));
        d.add(new Doc("a6", "alpha rewrite (9/17)",
                doc(1, 2, 3, 4, 5, 6, 7, 8, 9, 14, 15, 16, 17)));
        d.add(new Doc("a7", "alpha distant rewrite (8/18)",
                doc(1, 2, 3, 4, 5, 6, 7, 8, 14, 15, 16, 17, 18)));
        // Note a1 vs a7 = 8/18 = 0.444 < 0.6, yet they share a cluster via the
        // chain a1-a3-a4-a5-a6-a7 (every adjacent pair >= 0.6).
        d.add(new Doc("a8", "alpha too loose (10/16 but different drift, singleton)",
                doc(4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 21, 22, 23, 24, 25, 26)));
        // a8 vs a1 = 10/19 = 0.526 < 0.6 -> no edge; singleton (but LSH is
        // likely to surface it as a FALSE candidate that verification rejects).

        // ---- CHAIN: transitive, endpoints below threshold ----------------
        // Adjacent edges are exactly 0.6 (|inter|=6, union=10, 8-unit docs);
        // endpoint pair c1 vs c3 = 4/12 = 0.333 (< 0.5).
        d.add(new Doc("c1", "transitive chain left (8 units)", doc(31, 32, 33, 34, 35, 36, 37, 38)));
        d.add(new Doc("c2", "transitive chain middle (8 units)", doc(33, 34, 35, 36, 37, 38, 41, 42)));
        d.add(new Doc("c3", "transitive chain right (8 units)", doc(35, 36, 37, 38, 41, 42, 43, 44)));

        // ---- BETA: independent small near-duplicate pair -----------------
        d.add(new Doc("b1", "beta original", doc(51, 52, 53, 54, 55, 56)));
        d.add(new Doc("b2", "beta rewrite (5/7)", doc(51, 52, 53, 54, 55, 57)));
        // |inter|=5, union=7 -> 0.714

        // ---- ZH: Chinese near-duplicates (single-CJK-character tokens) ---
        // Every sentence has exactly 3 CJK characters, so k=3 yields exactly
        // one shingle per sentence and the set arithmetic is explicit.
        d.add(new Doc("z1", "chinese original", "今天气。 明天下。 我们一。 咖啡味。"));
        d.add(new Doc("z2", "chinese rewrite: 3 of 4 clauses shared",
                "今天气。 明天下。 我们一。 绿茶香。"));
        // z1 shingles {今天气, 明天下, 我们一, 咖啡味}
        // z2 shingles {今天气, 明天下, 我们一, 绿茶香}
        // -> 3 shared / 5 union = 0.6 exactly

        // ---- SHORT counter-examples --------------------------------------
        d.add(new Doc("s1", "short identical text A (only 2 CJK tokens -> 0 shingles)", "你好"));
        d.add(new Doc("s2", "short identical text B (only 2 CJK tokens -> 0 shingles)", "你好"));
        // Identical strings, but each segment has 2 CJK tokens < k=3, so both
        // have EMPTY shingle sets. Fixed rule: empty sets never match -> not one
        // cluster, even though the raw text is identical.
        d.add(new Doc("s3", "short doc sharing its only shingle, side 1",
                doc(61))); // 1 unit -> 1 shingle
        d.add(new Doc("s4", "short doc sharing its only shingle, side 2",
                doc(61, 63, 64, 65, 66))); // 5 units, 1 shared
        // Jaccard = 1/(1+5-1) = 1/5 = 0.2 -> no merge. Sharing the only
        // shingle of a tiny document must not be read as near-duplication
        // (containment would misfire here; Jaccard over the union does not).

        // ---- Unrelated singletons ----------------------------------------
        d.add(new Doc("x1", "unrelated topic x", doc(71, 72, 73, 74, 75)));
        d.add(new Doc("x2", "unrelated topic y", doc(81, 82, 83, 84, 85)));

        return d;
    }

    public static List<String> texts() {
        List<String> texts = new ArrayList<>();
        for (Doc d : corpus()) {
            texts.add(d.text());
        }
        return texts;
    }
}
