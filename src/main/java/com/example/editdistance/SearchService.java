package com.example.editdistance;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * Search pipeline: normalize query -&gt; lossless candidate filter -&gt; exact
 * code-point Levenshtein verification -&gt; sort and limit.
 *
 * <p>Because the filter is a necessary-condition filter, the verified result set is
 * exactly the set of corpus terms within distance k — identical to brute-force
 * scanning the whole corpus, only cheaper. The property tests assert this
 * equivalence against a full scan on random vocabularies.
 */
public final class SearchService {

    private final Normalize normalize;
    private volatile CandidateIndex index;

    public SearchService(Normalize normalize) {
        this.normalize = normalize;
        this.index = new CandidateIndex(List.of(), normalize);
    }

    public Normalize normalize() {
        return normalize;
    }

    /** Replaces the corpus and rebuilds the index. */
    public void rebuild(List<String> terms) {
        this.index = new CandidateIndex(terms, normalize);
    }

    public int corpusSize() {
        return index.size();
    }

    /**
     * Finds all corpus terms within {@code k} code-point edits of {@code query}.
     *
     * @param query raw query text (normalized inside)
     * @param k     edit-distance threshold, must be &gt;= 0
     * @param limit maximum number of matches returned (matches are sorted by
     *              distance then term, so the limit keeps the best ones)
     */
    public SearchResult search(String query, int k, int limit) {
        if (k < 0) {
            throw new IllegalArgumentException("k must be >= 0");
        }
        if (limit < 1) {
            throw new IllegalArgumentException("limit must be >= 1");
        }
        CandidateIndex current = index;
        String normalizedQuery = normalize.apply(query);
        int[] queryCps = normalizedQuery.codePoints().toArray();

        CandidateIndex.FilterResult filtered = current.filter(normalizedQuery, k);

        List<Match> all = new ArrayList<>();
        for (CandidateIndex.Candidate candidate : filtered.candidates()) {
            int distance = Levenshtein.distanceWithin(queryCps, candidate.codePoints(), k);
            if (distance <= k) {
                all.add(new Match(candidate.term(), distance));
            }
        }
        all.sort(Comparator.comparingInt(Match::distance).thenComparing(Match::term));

        List<Match> limited = all.size() > limit ? all.subList(0, limit) : all;
        return new SearchResult(
                query,
                normalizedQuery,
                k,
                current.size(),
                filtered.lengthPassed(),
                filtered.candidates().size(),
                all.size(),
                List.copyOf(limited));
    }
}
