package phraseindex;

import java.util.List;
import java.util.Set;

public final class InvertedIndexTest {

    public static void register(Suite s) {
        s.test("exact phrase match finds all starting positions, including repeated words", () -> {
            InvertedIndex idx = new InvertedIndex();
            idx.put(1, "go go go go");
            Query q = QueryParser.parse("\"go go go\"");
            Suite.assertEquals(Set.of(1), idx.matchingDocIds(q), "matches");
            Suite.assertEquals(List.of(0, 1), idx.phrasePositions(1, List.of("go", "go", "go")),
                    "all three start positions");
        });

        s.test("double spaces do not create phantom positions", () -> {
            InvertedIndex idx = new InvertedIndex();
            idx.put(1, "the  quick   brown fox"); // runs of spaces
            Suite.assertEquals(Set.of(1),
                    idx.matchingDocIds(QueryParser.parse("\"quick brown\"")), "adjacent in tokens");
            Suite.assertEquals(Set.of(1),
                    idx.matchingDocIds(QueryParser.parse("\"the quick\"")),
                    "runs of whitespace collapse; tokens still adjacent");
            Suite.assertEquals(Set.of(),
                    idx.matchingDocIds(QueryParser.parse("\"the brown\"")),
                    "non-adjacent tokens do not match");
        });

        s.test("empty document indexes nothing but exists and matches NOT", () -> {
            InvertedIndex idx = new InvertedIndex();
            idx.put(1, "");
            idx.put(2, "word");
            Suite.assertTrue(idx.contains(1), "empty doc exists");
            Suite.assertEquals(Set.of(2),
                    idx.matchingDocIds(QueryParser.parse("word")), "only non-empty doc found");
            Suite.assertEquals(Set.of(1),
                    idx.matchingDocIds(QueryParser.parse("NOT word")), "empty doc matches NOT word");
        });

        s.test("whitespace-only document behaves like empty", () -> {
            InvertedIndex idx = new InvertedIndex();
            idx.put(1, "   \t  ");
            Suite.assertTrue(idx.contains(1), "doc exists");
            Suite.assertEquals(Set.of(),
                    idx.matchingDocIds(QueryParser.parse("anything")), "no tokens");
        });

        s.test("phrase cannot match across document boundaries", () -> {
            InvertedIndex idx = new InvertedIndex();
            idx.put(1, "hello");
            idx.put(2, "world");
            Suite.assertEquals(Set.of(),
                    idx.matchingDocIds(QueryParser.parse("\"hello world\"")),
                    "no cross-document phrase");
            // Sanity: same words inside one document do match.
            idx.put(3, "hello world");
            Suite.assertEquals(Set.of(3),
                    idx.matchingDocIds(QueryParser.parse("\"hello world\"")),
                    "within-doc phrase matches");
        });

        s.test("replacing a document leaves no stale positions", () -> {
            InvertedIndex idx = new InvertedIndex();
            idx.put(1, "alpha beta alpha");
            idx.put(1, "gamma"); // replacement: alpha/beta positions must vanish
            Suite.assertEquals(Set.of(), idx.matchingDocIds(QueryParser.parse("alpha")), "alpha gone");
            Suite.assertEquals(Set.of(), idx.matchingDocIds(QueryParser.parse("beta")), "beta gone");
            Suite.assertEquals(Set.of(1), idx.matchingDocIds(QueryParser.parse("gamma")), "gamma present");
            Suite.assertFalse(idx.postings.containsKey("alpha")
                            && idx.postings.get("alpha").containsKey(1),
                    "no stale alpha posting for doc 1");
        });

        s.test("replacing with empty text removes all prior positions", () -> {
            InvertedIndex idx = new InvertedIndex();
            idx.put(1, "one two three");
            idx.put(1, "");
            Suite.assertTrue(idx.contains(1), "doc still exists");
            for (String term : new String[]{"one", "two", "three"}) {
                Suite.assertFalse(idx.postings.containsKey(term)
                                && idx.postings.get(term).containsKey(1),
                        "stale posting for " + term);
            }
        });

        s.test("replacing shared-term doc keeps other docs' positions", () -> {
            InvertedIndex idx = new InvertedIndex();
            idx.put(1, "cat dog");
            idx.put(2, "cat bird");
            idx.put(2, "dog fish");
            Suite.assertEquals(Set.of(1), idx.matchingDocIds(QueryParser.parse("cat")),
                    "only doc1 has cat");
            Suite.assertEquals(Set.of(1, 2), idx.matchingDocIds(QueryParser.parse("dog")),
                    "both docs have dog");
        });

        s.test("delete removes document and all its positions", () -> {
            InvertedIndex idx = new InvertedIndex();
            idx.put(1, "alpha beta");
            idx.put(2, "alpha gamma");
            Suite.assertTrue(idx.remove(1), "removed existing");
            Suite.assertFalse(idx.remove(1), "second remove is false");
            Suite.assertEquals(Set.of(2), idx.matchingDocIds(QueryParser.parse("alpha")),
                    "only doc2 posting survives");
            Suite.assertFalse(idx.postings.containsKey("beta"),
                    "term with no remaining docs is pruned");
        });

        s.test("boolean AND/OR/NOT semantics over documents", () -> {
            InvertedIndex idx = new InvertedIndex();
            idx.put(1, "cat dog");
            idx.put(2, "cat fish");
            idx.put(3, "fish");
            Suite.assertEquals(Set.of(1, 2),
                    idx.matchingDocIds(QueryParser.parse("cat")), "cat docs");
            Suite.assertEquals(Set.of(1),
                    idx.matchingDocIds(QueryParser.parse("cat AND dog")), "AND");
            Suite.assertEquals(Set.of(1, 2, 3),
                    idx.matchingDocIds(QueryParser.parse("cat OR fish")), "OR");
            Suite.assertEquals(Set.of(3),
                    idx.matchingDocIds(QueryParser.parse("fish AND NOT cat")), "NOT in AND");
            Suite.assertEquals(Set.of(3),
                    idx.matchingDocIds(QueryParser.parse("NOT cat")), "NOT alone");
        });

        s.test("phrase with all-same term requires consecutive occurrences", () -> {
            InvertedIndex idx = new InvertedIndex();
            idx.put(1, "ha ho ha");
            idx.put(2, "ha ha");
            Suite.assertEquals(Set.of(2),
                    idx.matchingDocIds(QueryParser.parse("\"ha ha\"")), "consecutive only");
            Suite.assertEquals(List.of(), idx.phrasePositions(1, List.of("ha", "ha")),
                    "non-consecutive repeats give no positions");
        });
    }
}
