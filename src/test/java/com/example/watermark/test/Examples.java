package com.example.watermark.test;

/**
 * Canonical script payloads, kept identical to the files under {@code examples/}.
 */
final class Examples {

    static final String PAUSE_RESUME = """
            {
              "config": {
                "maxOutOfOrdernessMillis": 0,
                "autoWatermarkIntervalMillis": 100,
                "idleTimeoutMillis": 300,
                "partitions": ["p1", "p2"]
              },
              "steps": [
                {"op": "event", "key": "p1", "timestamp": 1000},
                {"op": "event", "key": "p2", "timestamp": 1000},
                {"op": "advance", "time": 100},
                {"op": "event", "key": "p1", "timestamp": 2000},
                {"op": "advance", "time": 200},
                {"op": "event", "key": "p1", "timestamp": 3000},
                {"op": "advance", "time": 300},
                {"op": "event", "key": "p2", "timestamp": 500},
                {"op": "advance", "time": 400},
                {"op": "event", "key": "p1", "timestamp": 4000},
                {"op": "event", "key": "p2", "timestamp": 3500},
                {"op": "advance", "time": 500}
              ]
            }
            """;

    static final String FAST_PARTITION = """
            {
              "config": {
                "maxOutOfOrdernessMillis": 0,
                "autoWatermarkIntervalMillis": 100,
                "idleTimeoutMillis": 300000,
                "partitions": ["fast", "slow"]
              },
              "steps": [
                {"op": "event", "key": "fast", "timestamp": 1000000000000},
                {"op": "event", "key": "slow", "timestamp": 10},
                {"op": "advance", "time": 100},
                {"op": "event", "key": "fast", "timestamp": 2000000000000},
                {"op": "event", "key": "slow", "timestamp": 20},
                {"op": "advance", "time": 200},
                {"op": "event", "key": "slow", "timestamp": 30},
                {"op": "advance", "time": 300}
              ]
            }
            """;

    static final String WINDOWS = """
            {
              "config": {
                "maxOutOfOrdernessMillis": 0,
                "autoWatermarkIntervalMillis": 100,
                "idleTimeoutMillis": 60000,
                "windowSizeMillis": 1000,
                "partitions": ["p1", "p2"]
              },
              "steps": [
                {"op": "event", "key": "p1", "timestamp": 100, "value": "v1"},
                {"op": "event", "key": "p1", "timestamp": 900, "value": "v2"},
                {"op": "event", "key": "p2", "timestamp": 100, "value": "v3"},
                {"op": "advance", "time": 1000},
                {"op": "event", "key": "p2", "timestamp": 1100, "value": "v4"},
                {"op": "advance", "time": 1100},
                {"op": "event", "key": "p1", "timestamp": 2000, "value": "v5"},
                {"op": "advance", "time": 1200},
                {"op": "event", "key": "p1", "timestamp": 50, "value": "late-v6"}
              ]
            }
            """;

    private Examples() {
    }
}
