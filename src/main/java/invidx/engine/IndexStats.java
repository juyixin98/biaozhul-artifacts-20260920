package invidx.engine;

import java.util.List;

/** Read-only statistics about the live index. */
public record IndexStats(
        int publishedSegments,
        List<String> segmentNames,
        long tombstoneCount,
        int bufferedDocs,
        long segmentSeq,
        long manifestVersion,
        long liveDocCount) {
}
