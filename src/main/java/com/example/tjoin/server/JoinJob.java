package com.example.tjoin.server;

import com.example.tjoin.join.IntervalJoinOperator;
import com.example.tjoin.model.JoinConfig;
import com.example.tjoin.time.Clock;
import com.example.tjoin.time.ManualClock;
import com.example.tjoin.time.Scheduler;

/**
 * A running join job: operator plus the clock/scheduler driving it.
 * With {@code manualTime=true} the job uses a {@link ManualClock} (time
 * advances only via the service), which makes HTTP-level demos fully
 * deterministic; otherwise a system clock and real scheduler are used.
 */
public final class JoinJob {

    private final String id;
    private final JoinConfig config;
    private final ManualClock manualClock;
    private final Scheduler scheduler;
    private final IntervalJoinOperator operator;

    JoinJob(String id, JoinConfig config, ManualClock manualClock,
            Scheduler scheduler, IntervalJoinOperator operator) {
        this.id = id;
        this.config = config;
        this.manualClock = manualClock;
        this.scheduler = scheduler;
        this.operator = operator;
    }

    public String id() {
        return id;
    }

    public JoinConfig config() {
        return config;
    }

    public Clock clock() {
        return manualClock;
    }

    public ManualClock manualClock() {
        return manualClock;
    }

    public Scheduler scheduler() {
        return scheduler;
    }

    public IntervalJoinOperator operator() {
        return operator;
    }
}
