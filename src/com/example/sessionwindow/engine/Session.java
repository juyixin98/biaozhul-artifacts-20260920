package com.example.sessionwindow.engine;

import com.example.sessionwindow.model.Aggregate;

/**
 * Mutable per-session state for one key.
 *
 * <p>Sessions are kept sorted by start inside {@link SessionWindowEngine}. An
 * event at time {@code t} (its own window is {@code [t, t+gap]}) merges into a
 * session with span {@code [start, lastTimestamp+gap]} when the two intervals
 * overlap or touch at an endpoint:</p>
 *
 * <pre>
 *   start - gap &le; t &le; end   (end = lastTimestamp + gap)
 * </pre>
 *
 * <p>Both endpoints are inclusive, matching the offline rule "consecutive
 * events whose distance is exactly the gap stay in the same session". Two
 * anchor events farther apart than the gap (e.g. 0 and 15 with gap 10) start
 * separate sessions, and an event at 5 bridges them: its window [5,15]
 * touches the later span [15,25] at exactly 15.</p>
 */
final class Session {

    private Aggregate aggregate;
    private boolean sealed;
    private boolean published;

    Session(long timestamp, double value, long gap) {
        this.aggregate = Aggregate.single(timestamp, value, gap);
    }

    Session(Aggregate aggregate) {
        this.aggregate = aggregate;
    }

    long start() {
        return aggregate.start();
    }

    long end() {
        return aggregate.end();
    }

    Aggregate aggregate() {
        return aggregate;
    }

    boolean sealed() {
        return sealed;
    }

    void markSealed() {
        this.sealed = true;
    }

    /** A late event changed this session: its previous SEALED result is revoked. */
    void markReopened() {
        this.sealed = false;
    }

    boolean published() {
        return published;
    }

    void markPublished() {
        this.published = true;
    }

    void addEvent(long timestamp, double value, long gap) {
        aggregate = new Aggregate(
                Math.min(aggregate.start(), timestamp),
                Math.max(timestamp + gap, aggregate.end()),
                aggregate.count() + 1,
                aggregate.sum() + value,
                Math.min(aggregate.min(), value),
                Math.max(aggregate.max(), value));
    }

    void mergeFrom(Session other) {
        this.aggregate = this.aggregate.merge(other.aggregate);
        // Being touched by a new event reopens the merged session even if both
        // halves were previously sealed; their SEALED results are retracted.
        this.sealed = false;
    }

    boolean overlaps(long timestamp, long gap) {
        return timestamp >= start() - gap && timestamp <= end();
    }
}
