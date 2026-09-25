package com.example.tjoin.server;

import com.example.tjoin.join.IntervalJoinOperator;
import com.example.tjoin.model.JoinConfig;
import com.example.tjoin.time.ManualClock;

import java.util.Collection;
import java.util.List;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentMap;

/**
 * In-memory registry of named join jobs. Every job gets a manual clock, so
 * processing time is fully controlled through the HTTP API (no wall-clock
 * non-determinism in demos/tests).
 */
public final class JobRegistry {

    private final ConcurrentMap<String, JoinJob> jobs = new ConcurrentHashMap<>();

    public JoinJob create(String id, JoinConfig config) {
        ManualClock clock = new ManualClock(0L);
        IntervalJoinOperator operator = new IntervalJoinOperator(config, clock, clock);
        JoinJob job = new JoinJob(id, config, clock, clock, operator);
        JoinJob previous = jobs.putIfAbsent(id, job);
        if (previous != null) {
            throw new IllegalStateException("job already exists: " + id);
        }
        return job;
    }

    public JoinJob get(String id) {
        return jobs.get(id);
    }

    public boolean remove(String id) {
        return jobs.remove(id) != null;
    }

    public int size() {
        return jobs.size();
    }

    public Collection<JoinJob> all() {
        return List.copyOf(jobs.values());
    }
}
