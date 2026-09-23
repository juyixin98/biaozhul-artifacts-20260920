package com.example.qsketch.server;

import com.example.qsketch.QDigest;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/** In-memory, thread-safe registry of named summaries. Nothing is persisted. */
public final class SummaryStore {

    private final Map<String, QDigest> summaries = new ConcurrentHashMap<>();

    public QDigest create(String id, double eps, int universe) {
        QDigest d = new QDigest(eps, universe);
        QDigest existing = summaries.putIfAbsent(id, d);
        if (existing != null) {
            throw new IllegalStateException("summary '" + id + "' already exists");
        }
        return d;
    }

    public QDigest get(String id) {
        QDigest d = summaries.get(id);
        if (d == null) {
            throw new IllegalArgumentException("no summary named '" + id + "'");
        }
        return d;
    }

    public boolean exists(String id) {
        return summaries.containsKey(id);
    }

    public QDigest put(String id, QDigest digest) {
        QDigest existing = summaries.putIfAbsent(id, digest);
        if (existing != null) {
            throw new IllegalStateException("summary '" + id + "' already exists");
        }
        return digest;
    }

    public void delete(String id) {
        summaries.remove(id);
    }

    public Collection<String> ids() {
        return new ArrayList<>(summaries.keySet());
    }

    /**
     * Merge the named source summaries into target; all must be compatible.
     * Sources are snapshotted under their own locks one at a time (never
     * nested), then all snapshots are applied under the target's lock, so a
     * concurrent insert cannot corrupt iteration and there is no lock-order
     * deadlock.
     */
    public QDigest mergeInto(String targetId, List<String> sourceIds) {
        QDigest target = get(targetId);
        if (sourceIds.isEmpty()) {
            throw new IllegalArgumentException("merge requires at least one source");
        }
        // Resolve and compatibility-check up-front (all or nothing).
        List<QDigest> sources = new ArrayList<>();
        for (String sid : sourceIds) {
            QDigest s = get(sid);
            target.requireCompatible(s);
            sources.add(s);
        }
        List<QDigest.Snapshot> snapshots = new ArrayList<>();
        for (QDigest s : sources) {
            snapshots.add(s.snapshot());
        }
        synchronized (target) {
            for (QDigest.Snapshot snap : snapshots) {
                target.mergeSnapshot(snap.nodes(), snap.count());
            }
        }
        return target;
    }
}
