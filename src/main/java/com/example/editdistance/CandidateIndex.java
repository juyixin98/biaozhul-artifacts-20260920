package com.example.editdistance;

import java.util.ArrayList;
import java.util.Collection;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Lossless threshold candidate index for code-point Levenshtein distance.
 *
 * <p>Two filters are applied, both of which are <em>necessary</em> conditions for
 * {@code distance(s, t) <= k}, so the candidate set never misses a true match
 * (no false negatives):
 *
 * <ol>
 *   <li><b>Length filter</b>: {@code |len(s) - len(t)| <= k}, because each edit
 *       changes the length by at most one.</li>
 *   <li><b>Q-gram count filter</b> (q = 2, with q-1 sentinel padding on both ends):
 *       a string of length m has m + q - 1 positional q-grams, and a single edit
 *       destroys at most q of them, so if {@code distance(s, t) <= k} the multiset
 *       intersection of their q-grams has size at least
 *       {@code max(0, max(lenS, lenT) + q - 1 - k * q)}.</li>
 * </ol>
 *
 * <p>Gram overlap is computed exactly (per-gram {@code min} of multiset counts) via
 * an inverted index from gram to postings. Entries that pass the length filter but
 * share no grams with the query are still candidates whenever their threshold is 0.
 *
 * <p>The index only <em>filters</em>; callers must run exact verification
 * ({@link Levenshtein}) on the returned candidates, which removes all false
 * positives. Filtering is lossless, verification is exact.
 */
public final class CandidateIndex {

    private static final int Q = 2;
    private static final int SENTINEL = -1;

    /** One indexed term: normalized text plus its code points and gram multiset. */
    private static final class Entry {
        final String term;
        final int[] codePoints;
        final Map<Long, Integer> grams;

        Entry(String term) {
            this.term = term;
            this.codePoints = term.codePoints().toArray();
            this.grams = gramsOf(codePoints);
        }
    }

    /** A candidate term handed to the exact-verification stage. */
    public record Candidate(String term, int[] codePoints) {
    }

    /** Filter output: candidates plus how many entries passed the length filter. */
    public record FilterResult(List<Candidate> candidates, int lengthPassed) {
    }

    private final List<Entry> entries = new ArrayList<>();
    private final Map<Long, List<Integer>> inverted = new HashMap<>();

    /**
     * Builds an index over the given terms after normalization. Terms that are
     * identical after normalization are indexed once (first occurrence wins).
     */
    public CandidateIndex(Collection<String> terms, Normalize normalize) {
        Map<String, String> deduped = new LinkedHashMap<>();
        for (String term : terms) {
            String normalized = normalize.apply(term);
            deduped.putIfAbsent(normalized, normalized);
        }
        for (String normalized : deduped.keySet()) {
            add(new Entry(normalized));
        }
    }

    private void add(Entry entry) {
        int id = entries.size();
        entries.add(entry);
        for (Long gram : entry.grams.keySet()) {
            inverted.computeIfAbsent(gram, g -> new ArrayList<>()).add(id);
        }
    }

    public int size() {
        return entries.size();
    }

    /**
     * Returns all index entries that can possibly be within {@code k} edits of
     * {@code normalizedQuery}. The query must already be normalized with the same
     * policy used at build time.
     */
    public FilterResult filter(String normalizedQuery, int k) {
        if (k < 0) {
            throw new IllegalArgumentException("k must be >= 0");
        }
        int[] queryCps = normalizedQuery.codePoints().toArray();
        int m = queryCps.length;
        Map<Long, Integer> queryGrams = gramsOf(queryCps);

        // Length filter over all entries; gram overlap accumulated via inverted index.
        boolean[] lengthOk = new boolean[entries.size()];
        int lengthPassed = 0;
        for (int i = 0; i < entries.size(); i++) {
            if (Math.abs(entries.get(i).codePoints.length - m) <= k) {
                lengthOk[i] = true;
                lengthPassed++;
            }
        }

        Map<Integer, Integer> overlap = new HashMap<>();
        for (Map.Entry<Long, Integer> queryGram : queryGrams.entrySet()) {
            List<Integer> postings = inverted.get(queryGram.getKey());
            if (postings == null) {
                continue;
            }
            int queryCount = queryGram.getValue();
            for (int id : postings) {
                if (!lengthOk[id]) {
                    continue;
                }
                int entryCount = entries.get(id).grams.get(queryGram.getKey());
                overlap.merge(id, Math.min(queryCount, entryCount), Integer::sum);
            }
        }

        List<Candidate> candidates = new ArrayList<>();
        for (int i = 0; i < entries.size(); i++) {
            if (!lengthOk[i]) {
                continue;
            }
            Entry entry = entries.get(i);
            int threshold = gramThreshold(Math.max(m, entry.codePoints.length), k);
            if (overlap.getOrDefault(i, 0) >= threshold) {
                candidates.add(new Candidate(entry.term, entry.codePoints));
            }
        }
        return new FilterResult(candidates, lengthPassed);
    }

    /** Minimum q-gram multiset overlap required when the longer string has length maxLen. */
    static int gramThreshold(int maxLen, int k) {
        return Math.max(0, maxLen + Q - 1 - k * Q);
    }

    /** Positional q-grams (q = 2) over code points, padded with one sentinel on each side. */
    static Map<Long, Integer> gramsOf(int[] codePoints) {
        Map<Long, Integer> grams = new HashMap<>();
        long prev = SENTINEL;
        for (int cp : codePoints) {
            grams.merge(gramKey(prev, cp), 1, Integer::sum);
            prev = cp;
        }
        grams.merge(gramKey(prev, SENTINEL), 1, Integer::sum);
        return grams;
    }

    private static long gramKey(long first, long second) {
        return (first << 32) | (second & 0xFFFFFFFFL);
    }
}
