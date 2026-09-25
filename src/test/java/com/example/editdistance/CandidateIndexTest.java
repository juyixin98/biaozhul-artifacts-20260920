package com.example.editdistance;

import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * Acceptance tests: the candidate index plus exact verification must produce
 * exactly the same results as brute-force scanning every term with the exact
 * distance — for random vocabularies, empty strings, combining characters, and
 * long common prefixes. No threshold-internal result may be missed.
 */
class CandidateIndexTest {

    /** Brute-force reference: normalize, dedupe, exact distance for every term. */
    private static List<Match> bruteForce(List<String> corpus, Normalize normalize,
                                          String query, int k) {
        String nq = normalize.apply(query);
        int[] qcps = nq.codePoints().toArray();
        Set<String> seen = new LinkedHashSet<>();
        List<Match> out = new ArrayList<>();
        for (String term : corpus) {
            String nt = normalize.apply(term);
            if (!seen.add(nt)) {
                continue;
            }
            int d = Levenshtein.distance(qcps, nt.codePoints().toArray());
            if (d <= k) {
                out.add(new Match(nt, d));
            }
        }
        out.sort(java.util.Comparator.comparingInt(Match::distance).thenComparing(Match::term));
        return out;
    }

    private static void assertSameResults(List<String> corpus, Normalize normalize,
                                          String query, int k) {
        SearchService service = new SearchService(normalize);
        service.rebuild(corpus);
        SearchResult viaIndex = service.search(query, k, Integer.MAX_VALUE);
        List<Match> expected = bruteForce(corpus, normalize, query, k);
        assertEquals(expected, viaIndex.matches(),
                "mismatch for query=" + query + " k=" + k + " normalize=" + normalize);
    }

    @Test
    void handPickedEdgeCases() {
        List<String> corpus = List.of(
                "", "a", "ab", "abc", "abd", "kitten", "sitting",
                "caf\u00E9", "cafe\u0301", "编辑距离", "编辑",
                "😀", "😃", "😀😃", "x".repeat(100), "x".repeat(100) + "y",
                "the-quick-brown-fox-jumps-over-the-lazy-dog-v1",
                "the-quick-brown-fox-jumps-over-the-lazy-dog-v2");
        for (Normalize normalize : List.of(Normalize.NFC, Normalize.NONE, Normalize.NFD)) {
            for (String query : List.of("", "a", "abc", "abd", "caf\u00E9", "cafe\u0301",
                    "编辑", "😀", "😀😀", "x".repeat(99),
                    "the-quick-brown-fox-jumps-over-the-lazy-dog-v3")) {
                for (int k = 0; k <= 3; k++) {
                    assertSameResults(corpus, normalize, query, k);
                }
            }
        }
    }

    @Test
    void randomVocabularyMatchesBruteForce() {
        Random random = new Random(20260924L);
        String alphabet = "abcdeé中文字😀😃 ";
        int[] alphabetCps = alphabet.codePoints().toArray();

        for (int trial = 0; trial < 30; trial++) {
            // Random small vocabulary, sometimes including the empty string.
            List<String> corpus = new ArrayList<>();
            if (random.nextBoolean()) {
                corpus.add("");
            }
            int vocabSize = 1 + random.nextInt(40);
            for (int i = 0; i < vocabSize; i++) {
                corpus.add(randomString(random, alphabetCps, 12));
            }
            // Sprinkle long-common-prefix families.
            String prefix = randomString(random, alphabetCps, 30);
            for (int i = 0; i < 3; i++) {
                corpus.add(prefix + randomString(random, alphabetCps, 3));
            }

            Normalize normalize = random.nextBoolean() ? Normalize.NFC : Normalize.NONE;
            for (int q = 0; q < 10; q++) {
                String query = randomString(random, alphabetCps, 10);
                int k = random.nextInt(4);
                assertSameResults(corpus, normalize, query, k);
            }
        }
    }

    @Test
    void filterNeverMissesTrueMatch() {
        // Direct check of the no-false-negative contract on the generated corpus:
        // every term within k of the query must appear in the candidate set.
        List<String> corpus = CorpusGenerator.generate(500, 7L);
        SearchService service = new SearchService(Normalize.NFC);
        service.rebuild(corpus);
        CandidateIndex index = new CandidateIndex(corpus, Normalize.NFC);

        Random random = new Random(99L);
        for (int trial = 0; trial < 200; trial++) {
            String query = corpus.get(random.nextInt(corpus.size()));
            int k = random.nextInt(4);
            String nq = Normalize.NFC.apply(query);
            int[] qcps = nq.codePoints().toArray();

            Set<String> candidateTerms = new LinkedHashSet<>();
            for (CandidateIndex.Candidate c : index.filter(nq, k).candidates()) {
                candidateTerms.add(c.term());
            }
            Set<String> seen = new LinkedHashSet<>();
            for (String term : corpus) {
                String nt = Normalize.NFC.apply(term);
                if (!seen.add(nt)) {
                    continue;
                }
                int d = Levenshtein.distance(qcps, nt.codePoints().toArray());
                if (d <= k) {
                    assertTrue(candidateTerms.contains(nt),
                            "filter missed term=" + nt + " query=" + nq + " k=" + k + " d=" + d);
                }
            }
        }
    }

    @Test
    void generatedCorpusEndToEnd() {
        List<String> corpus = CorpusGenerator.generate(1000, 42L);
        for (String query : List.of("apple", "appl", "编辑", "caf\u00E9", "cafe\u0301", "😀", "")) {
            for (int k = 0; k <= 3; k++) {
                assertSameResults(corpus, Normalize.NFC, query, k);
            }
        }
    }

    private static String randomString(Random random, int[] alphabetCps, int maxLen) {
        int len = random.nextInt(maxLen + 1);
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < len; i++) {
            sb.appendCodePoint(alphabetCps[random.nextInt(alphabetCps.length)]);
        }
        return sb.toString();
    }
}
