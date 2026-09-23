package dedup;

/**
 * One retained event-id entry: the id together with the event timestamp it is
 * anchored to. A duplicate carrying a later event time lifts the anchor, which
 * only ever extends the retention of the id (never shortens it).
 */
public final class DedupEntry {
    private final String eventId;
    private long anchorEventTime;

    public DedupEntry(String eventId, long anchorEventTime) {
        this.eventId = eventId;
        this.anchorEventTime = anchorEventTime;
    }

    public String eventId() {
        return eventId;
    }

    public long anchorEventTime() {
        return anchorEventTime;
    }

    void liftAnchorTo(long eventTime) {
        if (eventTime > anchorEventTime) {
            anchorEventTime = eventTime;
        }
    }
}
