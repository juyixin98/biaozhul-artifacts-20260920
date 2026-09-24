package phraseindex;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.Set;

/**
 * Acceptance differential test.
 *
 * Runs a stream of random insert/replace/delete operations, then checks
 * <em>every</em> query two ways:
 * <ol>
 *   <li>the positional inverted index ({@link InvertedIndex});</li>
 *   <li>a brute-force per-document scanning judge ({@link BruteForceJudge}).</li>
 * </ol>
 *
 * It additionally walks the internal posting tables and asserts they match a
 * freshly rebuilt expectation, so a replacement can never leave stale
 * positions behind.
 */
public final class DifferentialTest {

    private static final String[] VOCAB = {
            "alpha", "beta", "gamma", "go", "ha", "cat", "dog", "fish",
            "bird", "quick", "brown", "fox", "absent1", "absent2"
    };

    private static final String[] FIXED_QUERIES = {
            "\"go go go\"",
            "\"go go\"",
            "\"ha ha\"",
            "go",
            "go AND go",
            "go OR ha",
            "NOT go",
            "(cat OR dog) AND NOT fish",
            "\"quick brown\"",
            "\"\"",
            "absent1",
            "NOT absent1",
            "alpha AND NOT beta",
    };

    public static void register(Suite s) {
        s.test("random operations: index answers equal per-document scanning judge", () -> {
            Random rnd = new Random(20260924L);
            InvertedIndex index = new InvertedIndex();
            Map<Integer, String> mirror = new HashMap<>();

            for (int iteration = 0; iteration < 300; iteration++) {
                applyRandomOperation(index, mirror, rnd);

                if (iteration % 10 == 0) {
                    // Internals must always be exactly what a rebuild yields.
                    assertNoStalePositions(index, mirror);
                }

                BruteForceJudge judge = new BruteForceJudge(mirror);

                for (String qs : FIXED_QUERIES) {
                    Query q = QueryParser.parse(qs);
                    Set<Integer> expected = judge.matchingDocIds(q);
                    Set<Integer> actual = index.matchingDocIds(q);
                    Suite.assertEquals(expected, actual,
                            "fixed query [" + qs + "] at iteration " + iteration
                                    + " docs=" + mirror.keySet());
                    // Position-level check for top-level phrases.
                    if (q instanceof Query.Phrase p && !p.terms().isEmpty()) {
                        assertPhrasePositions(index, mirror, p.terms());
                    }
                }

                for (int q = 0; q < 40; q++) {
                    Query randomQuery = randomQuery(rnd, 2);
                    Set<Integer> expected = judge.matchingDocIds(randomQuery);
                    Set<Integer> actual = index.matchingDocIds(randomQuery);
                    Suite.assertEquals(expected, actual,
                            "random query " + randomQuery + " at iteration " + iteration);
                }
            }
        });
    }

    private static void applyRandomOperation(InvertedIndex index, Map<Integer, String> mirror,
                                             Random rnd) {
        int docId = rnd.nextInt(8);
        int action = rnd.nextInt(10);
        if (action < 7) {
            String text = randomDocument(rnd);
            index.put(docId, text);
            mirror.put(docId, text);
        } else if (action == 7) {
            // Force empty / whitespace-only documents often enough to matter.
            String text = rnd.nextBoolean() ? "" : "    \t  ";
            index.put(docId, text);
            mirror.put(docId, text);
        } else if (action == 8) {
            // Replace with a single repeated-word text (phrase edge case).
            String word = VOCAB[rnd.nextInt(6)];
            int repeats = 1 + rnd.nextInt(4);
            String text = String.join(" ", repeated(word, repeats));
            index.put(docId, text);
            mirror.put(docId, text);
        } else {
            index.remove(docId);
            mirror.remove(docId);
        }
    }

    private static String[] repeated(String word, int n) {
        String[] arr = new String[n];
        java.util.Arrays.fill(arr, word);
        return arr;
    }

    private static String randomDocument(Random rnd) {
        int length = rnd.nextInt(7); // includes 0 tokens
        List<String> parts = new ArrayList<>();
        for (int i = 0; i < length; i++) {
            parts.add(VOCAB[rnd.nextInt(VOCAB.length - 2)]);
        }
        // Wrap in random whitespace, including runs of spaces, to exercise
        // the fixed tokenization rule. Between tokens there is always at
        // least one whitespace; only the amount varies.
        StringBuilder sb = new StringBuilder();
        appendSpaces(rnd, sb);
        for (int i = 0; i < parts.size(); i++) {
            sb.append(parts.get(i));
            sb.append(' ');
            appendSpaces(rnd, sb);
        }
        return sb.toString();
    }

    private static final char[] WHITESPACE = {' ', ' ', ' ', '\t', '\n'};

    private static void appendSpaces(Random rnd, StringBuilder sb) {
        int n = rnd.nextInt(4);
        for (int i = 0; i < n; i++) {
            sb.append(WHITESPACE[rnd.nextInt(WHITESPACE.length)]);
        }
    }

    private static Query randomQuery(Random rnd, int depth) {
        int kind;
        if (depth <= 0) {
            kind = rnd.nextInt(2); // phrase only at leaf level
        } else {
            kind = rnd.nextInt(5);
        }
        return switch (kind) {
            case 0 -> new Query.Phrase(List.of(VOCAB[rnd.nextInt(VOCAB.length)]));
            case 1 -> {
                int n = 1 + rnd.nextInt(3);
                List<String> terms = new ArrayList<>();
                for (int i = 0; i < n; i++) {
                    terms.add(VOCAB[rnd.nextInt(VOCAB.length)]);
                }
                yield new Query.Phrase(terms);
            }
            case 2 -> new Query.And(randomQuery(rnd, depth - 1), randomQuery(rnd, depth - 1));
            case 3 -> new Query.Or(randomQuery(rnd, depth - 1), randomQuery(rnd, depth - 1));
            default -> new Query.Not(randomQuery(rnd, depth - 1));
        };
    }

    /** Rebuilds expected postings from scratch and compares every entry. */
    private static void assertNoStalePositions(InvertedIndex index, Map<Integer, String> mirror) {
        Map<String, Map<Integer, List<Integer>>> expected = new HashMap<>();
        for (Map.Entry<Integer, String> e : mirror.entrySet()) {
            List<String> tokens = Tokenizer.tokenize(e.getValue());
            for (int pos = 0; pos < tokens.size(); pos++) {
                expected.computeIfAbsent(tokens.get(pos), k -> new HashMap<>())
                        .computeIfAbsent(e.getKey(), k -> new ArrayList<>())
                        .add(pos);
            }
        }
        Suite.assertEquals(expected.keySet(), index.postings.keySet(),
                "posting term set mismatch");
        for (Map.Entry<String, Map<Integer, List<Integer>>> termEntry : expected.entrySet()) {
            Map<Integer, List<Integer>> actualByDoc = index.postings.get(termEntry.getKey());
            Suite.assertEquals(termEntry.getValue().keySet(), actualByDoc.keySet(),
                    "doc set for term " + termEntry.getKey());
            for (Map.Entry<Integer, List<Integer>> docEntry : termEntry.getValue().entrySet()) {
                Suite.assertEquals(docEntry.getValue(), actualByDoc.get(docEntry.getKey()),
                        "positions for term=" + termEntry.getKey() + " doc=" + docEntry.getKey());
            }
        }
        Suite.assertEquals(mirror.size(), index.size(), "document count");
    }

    private static void assertPhrasePositions(InvertedIndex index, Map<Integer, String> mirror,
                                              List<String> terms) {
        for (Map.Entry<Integer, String> e : mirror.entrySet()) {
            List<String> tokens = Tokenizer.tokenize(e.getValue());
            List<Integer> expected = BruteForceJudge.phrasePositions(tokens, terms);
            List<Integer> actual = index.phrasePositions(e.getKey(), terms);
            Suite.assertEquals(expected, actual,
                    "phrase positions " + terms + " in doc " + e.getKey());
        }
    }
}
