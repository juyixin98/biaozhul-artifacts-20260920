package com.example.dedup.time;

/** Injectable clock: wall-time in milliseconds (epoch millis semantics). */
public interface Clock {
    long currentTimeMillis();
}
