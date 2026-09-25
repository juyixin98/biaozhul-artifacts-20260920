package invidx.catalog;

import com.fasterxml.jackson.annotation.JsonProperty;
import invidx.segment.SegmentInfo;

import java.util.List;
import java.util.Map;

/** JSON-serializable view of {@code manifest.json}. */
public record Manifest(
        @JsonProperty("version") long version,
        @JsonProperty("segmentSeq") long segmentSeq,
        @JsonProperty("nextDocId") int nextDocId,
        @JsonProperty("nextGen") Map<Integer, Long> nextGen,
        @JsonProperty("segments") List<SegmentInfo> segments) {

    public Manifest {
        nextGen = nextGen == null ? Map.of() : Map.copyOf(nextGen);
        segments = segments == null ? List.of() : List.copyOf(segments);
    }
}
