package dedup;

import java.util.List;

/**
 * Immutable state snapshot handed from an old owner (source partition) to a new
 * owner (destination partition) during migration. The routing version is the
 * epoch the source had when exported; the destination must install it under a
 * strictly greater epoch.
 */
public record Snapshot(String partitionKey,
                       long epoch,
                       long watermark,
                       long retentionMillis,
                       List<EntryView> entries) {

    /** JSON-friendly view of one retained entry. */
    public record EntryView(String eventId, long anchorEventTime) {
    }
}
