package cep.model;

/** 一次成功的 A→B 匹配，记录所用事件的 ID，便于核对。 */
public final class Match {

    private final String aId;
    private final String bId;
    private final long aTimestamp;
    private final long bTimestamp;
    private final long matchedAtWatermark;
    private final boolean late;

    public Match(String aId, String bId, long aTimestamp, long bTimestamp,
                 long matchedAtWatermark, boolean late) {
        this.aId = aId;
        this.bId = bId;
        this.aTimestamp = aTimestamp;
        this.bTimestamp = bTimestamp;
        this.matchedAtWatermark = matchedAtWatermark;
        this.late = late;
    }

    public String aId() { return aId; }
    public String bId() { return bId; }
    public long aTimestamp() { return aTimestamp; }
    public long bTimestamp() { return bTimestamp; }
    public long matchedAtWatermark() { return matchedAtWatermark; }
    public boolean late() { return late; }

    @Override
    public String toString() {
        return "Match[" + aId + " -> " + bId + ", dt=" + (bTimestamp - aTimestamp)
                + (late ? ", late" : "") + "]";
    }
}
