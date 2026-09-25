package approxheavy.server;

import approxheavy.core.Clock;
import approxheavy.core.Scheduler;
import approxheavy.stream.EventStreamProcessor;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/** Holds named stream processors (one stream = one tumbling-window ingestion job). */
public final class StreamRegistry {
    public static final class Config {
        public int width = 32;
        public int depth = 5;
        public long seed = 0L;
        public int candidateCapacity = 64;
        public long windowMillis = 60_000L;
        public int retainedWindows = 10;
    }

    private final Map<String, EventStreamProcessor> streams = new ConcurrentHashMap<>();
    private final Clock clock;
    private final Scheduler scheduler;

    public StreamRegistry(Clock clock, Scheduler scheduler) {
        this.clock = clock;
        this.scheduler = scheduler;
    }

    public EventStreamProcessor create(String name, Config config) {
        EventStreamProcessor processor = new EventStreamProcessor(
                config.width, config.depth, config.seed, config.candidateCapacity,
                config.windowMillis, config.retainedWindows, clock, scheduler);
        EventStreamProcessor existing = streams.putIfAbsent(name, processor);
        if (existing != null) {
            throw new IllegalArgumentException("stream '" + name + "' already exists");
        }
        return processor;
    }

    public EventStreamProcessor require(String name) {
        EventStreamProcessor processor = streams.get(name);
        if (processor == null) {
            throw new NotFoundException("stream '" + name + "' not found");
        }
        return processor;
    }

    public boolean exists(String name) {
        return streams.containsKey(name);
    }

    public Map<String, EventStreamProcessor> all() {
        return streams;
    }
}
