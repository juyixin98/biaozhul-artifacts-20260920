package com.example.tjoin.time;

/**
 * Injectable clock. Two implementations are provided:
 * {@link SystemClock} (wall-clock production) and {@link ManualClock}
 * (deterministic tests and service-driven time). The join library never
 * calls {@link System#currentTimeMillis()} directly.
 */
@FunctionalInterface
public interface Clock {

    long currentTimeMillis();
}
