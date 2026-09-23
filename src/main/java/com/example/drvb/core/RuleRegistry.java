package com.example.drvb.core;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The version timeline: an append-only, ordered list of immutable
 * {@link RuleVersion}s, keyed by event time.
 *
 * <h2>Publication contract</h2>
 * <ul>
 *   <li>The first version is installed by {@link #bootstrap(RuleVersion)} and
 *       its {@code effectiveFrom} is normalized to {@link Long#MIN_VALUE}:
 *       the bootstrap rule set covers all history for which no later version
 *       exists.</li>
 *   <li>Every subsequent version must have an {@code effectiveFrom} strictly
 *       greater than the previous version's effective time. Versions may be
 *       <b>registered in advance</b> (an effectiveFrom in the processing
 *       future): binding answers stay deterministic because an event at time
 *       {@code t} only ever sees versions whose {@code effectiveFrom <= t},
 *       and no version's interval can overlap or alter an already-closed
 *       time. The current watermark is therefore not a publication gate; it
 *       only drives garbage collection.</li>
 *   <li>Published versions are never mutated. A "rollback" appends a
 *       <b>new</b> version whose rules are copied from an earlier version
 *       (identical {@code checksum}, new {@code versionId}).</li>
 * </ul>
 *
 * <h2>Lookup contract</h2>
 * {@link #resolve(long, long, long)} selects the latest version whose
 * {@code effectiveFrom <= eventTime}. It never silently substitutes another
 * version:
 * <ul>
 *   <li>before bootstrap: {@link Missing} — callers must reject the event;</li>
 *   <li>event older than the earliest retained version's start (which only
 *       happens after garbage collection and therefore after the event is
 *       beyond the lateness horizon): {@link Reclaimed};</li>
 *   <li>otherwise: {@link Bound}, including for late events, which are bound
 *       to the historical version in force at their event time.</li>
 * </ul>
 */
public final class RuleRegistry {

    /** Outcome of resolving an event time against the timeline. */
    public sealed interface Resolution permits Bound, Reclaimed, Missing {
    }

    /** A usable historical/current version bound to the event. */
    public record Bound(RuleVersion version) implements Resolution {
    }

    /** The needed version was garbage collected; event is beyond the lateness horizon. */
    public record Reclaimed(String oldestRetainedVersionId, long oldestRetainedFrom)
            implements Resolution {
    }

    /** No version exists at or before the event time (typically pre-bootstrap). */
    public record Missing() implements Resolution {
    }

    private final List<RuleVersion> versions = new ArrayList<>();
    private final Map<String, Integer> indexById = new LinkedHashMap<>();
    private long watermark = Long.MIN_VALUE;

    // ----------------------------------------------------------- publication

    /**
     * Installs the initial rule set. Exactly one bootstrap is allowed.
     * The supplied version's {@code effectiveFrom} is ignored and normalized
     * to {@link Long#MIN_VALUE}.
     */
    public synchronized RuleVersion bootstrap(RuleVersion v) {
        if (!versions.isEmpty()) {
            throw new RuleRegistryException(RuleErrorCode.ALREADY_BOOTSTRAPPED,
                    "registry already bootstrapped at version "
                            + versions.get(0).versionId());
        }
        RuleVersion normalized = new RuleVersion(v.versionId(), Long.MIN_VALUE,
                v.createdAt(), v.description(), v.rules());
        add(normalized);
        return normalized;
    }

    /**
     * Appends a new immutable version. Its {@code effectiveFrom} must be
     * strictly greater than the latest version's effective time; it may lie in
     * the future relative to the current watermark (advance registration).
     */
    public synchronized RuleVersion publish(RuleVersion v) {
        if (versions.isEmpty()) {
            throw new RuleRegistryException(RuleErrorCode.NOT_BOOTSTRAPPED,
                    "call bootstrap before publishing versions");
        }
        long lastFrom = versions.get(versions.size() - 1).effectiveFrom();
        if (v.effectiveFrom() <= lastFrom) {
            throw new RuleRegistryException(RuleErrorCode.EFFECTIVE_TIME_IN_PAST,
                    "effectiveFrom " + v.effectiveFrom()
                            + " must be strictly greater than the previous version's start "
                            + lastFrom);
        }
        add(v);
        return v;
    }

    /**
     * Appends a new version that restores the rule content of an earlier
     * version. The source version is left untouched; only its rules are
     * copied. The new id must be fresh and the usual effective-time gate
     * applies.
     */
    public synchronized RuleVersion rollback(String sourceVersionId,
                                             String newVersionId,
                                             long effectiveFrom,
                                             long createdAt,
                                             String description) {
        Integer idx = indexById.get(sourceVersionId);
        if (idx == null) {
            throw new RuleRegistryException(RuleErrorCode.SOURCE_VERSION_NOT_FOUND,
                    "source version not found or reclaimed: " + sourceVersionId);
        }
        RuleVersion source = versions.get(idx);
        RuleVersion copy = new RuleVersion(newVersionId, effectiveFrom, createdAt,
                description == null
                        ? "rollback of " + sourceVersionId
                        : description,
                source.rules());
        return publish(copy);
    }

    private void add(RuleVersion v) {
        if (indexById.containsKey(v.versionId())) {
            throw new RuleRegistryException(RuleErrorCode.DUPLICATE_VERSION,
                    "version id already exists: " + v.versionId());
        }
        versions.add(v);
        indexById.put(v.versionId(), versions.size() - 1);
    }

    // -------------------------------------------------------------- lookup

    /**
     * Resolves the rule version in force at {@code eventTime}: the rightmost
     * version whose {@code effectiveFrom} is not greater than the event time.
     * Never substitutes another version in its place:
     * <ul>
     *   <li>before bootstrap: {@link Missing} — callers must reject the event;</li>
     *   <li>event older than the earliest retained version's start (only
     *       possible after garbage collection): {@link Reclaimed};</li>
     *   <li>otherwise: {@link Bound}, including for late events, which bind to
     *       the historical version in force at their event time.</li>
     * </ul>
     */
    public synchronized Resolution resolve(long eventTime) {
        if (versions.isEmpty()) {
            return new Missing();
        }
        RuleVersion oldest = versions.get(0);
        // Reclamation only ever removes a prefix, and every removed version's
        // whole interval ended at or before the lateness horizon; hence an
        // event older than the oldest retained start needs a version that was
        // already collected, not "the closest remaining" one.
        if (eventTime < oldest.effectiveFrom()) {
            return new Reclaimed(oldest.versionId(), oldest.effectiveFrom());
        }
        // Binary search for the rightmost version with effectiveFrom <= eventTime.
        int lo = 0;
        int hi = versions.size() - 1;
        while (lo < hi) {
            int mid = (lo + hi + 1) >>> 1;
            if (versions.get(mid).effectiveFrom() <= eventTime) {
                lo = mid;
            } else {
                hi = mid - 1;
            }
        }
        return new Bound(versions.get(lo));
    }

    /** Saturating subtraction; {@code Long.MIN_VALUE} represents "no bound". */
    public static long saturatingSubtract(long a, long b) {
        long r = a - b;
        // Detect underflow; Long.MIN_VALUE means "no bound".
        if (b > 0 && a < Long.MIN_VALUE + b) {
            return Long.MIN_VALUE;
        }
        return r;
    }

    // ------------------------------------------------------------- GC hooks

    /**
     * Reclaims the maximal eligible prefix in one pass. Returns the removed
     * versions. The latest version is always retained.
     */
    public synchronized List<RuleVersion> reclaim(long watermark, long allowedLateness,
                                           java.util.Set<String> referencedVersionIds) {
        this.watermark = watermark;
        long horizon = saturatingSubtract(watermark, allowedLateness);
        List<RuleVersion> removed = new ArrayList<>();
        while (versions.size() > 1) {
            RuleVersion candidate = versions.get(0);
            RuleVersion next = versions.get(1);
            boolean unreferenced = !referencedVersionIds.contains(candidate.versionId());
            if (next.effectiveFrom() <= horizon && unreferenced) {
                removed.add(candidate);
                versions.remove(0);
            } else {
                break;
            }
        }
        rebuildIndex();
        return removed;
    }

    private void rebuildIndex() {
        indexById.clear();
        for (int i = 0; i < versions.size(); i++) {
            indexById.put(versions.get(i).versionId(), i);
        }
    }

    /** Updates the watermark gate used by {@link #publish(RuleVersion)}. */
    public synchronized void noteWatermark(long w) {
        if (w > watermark) {
            watermark = w;
        }
    }

    // ------------------------------------------------------------- readers

    public synchronized boolean isBootstrapped() {
        return !versions.isEmpty();
    }

    public synchronized RuleVersion latest() {
        return versions.isEmpty() ? null : versions.get(versions.size() - 1);
    }

    public synchronized RuleVersion get(String versionId) {
        Integer idx = indexById.get(versionId);
        return idx == null ? null : versions.get(idx);
    }

    public synchronized List<RuleVersion> versions() {
        return List.copyOf(versions);
    }

    public synchronized long watermark() {
        return watermark;
    }

    /** Versions that would be reclaimed by a GC call with the given horizon;
     *  does not mutate state. */
    public synchronized List<RuleVersion> reclaimPreview(long watermark,
                                                          long allowedLateness,
                                                          java.util.Set<String> referencedVersionIds) {
        long horizon = saturatingSubtract(watermark, allowedLateness);
        List<RuleVersion> preview = new ArrayList<>();
        for (int i = 0; i < versions.size() - 1; i++) {
            if (versions.get(i + 1).effectiveFrom() <= horizon
                    && !referencedVersionIds.contains(versions.get(i).versionId())) {
                preview.add(versions.get(i));
            } else {
                break;
            }
        }
        return Collections.unmodifiableList(preview);
    }
}
