package invidx.segment;

/** Metadata describing one published segment, as recorded in the manifest. */
public record SegmentInfo(String name, int docs, boolean merged) {
}
