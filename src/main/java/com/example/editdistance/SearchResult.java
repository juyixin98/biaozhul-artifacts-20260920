package com.example.editdistance;

import java.util.List;

/**
 * Result of one search: verified matches (sorted by distance, then term) plus
 * funnel statistics showing how many entries survived each stage.
 *
 * @param corpusSize   total distinct normalized terms in the index
 * @param lengthPassed entries surviving the length filter
 * @param candidates   entries surviving the q-gram count filter (sent to exact DP)
 * @param query        the query as received
 * @param normalizedQuery the query after normalization
 * @param k            edit-distance threshold used
 * @param matches      verified matches, possibly truncated to the requested limit
 * @param totalMatches number of verified matches before the limit was applied
 */
public record SearchResult(
        String query,
        String normalizedQuery,
        int k,
        int corpusSize,
        int lengthPassed,
        int candidates,
        int totalMatches,
        List<Match> matches) {
}
