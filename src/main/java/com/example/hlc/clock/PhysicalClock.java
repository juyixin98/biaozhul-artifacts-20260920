package com.example.hlc.clock;

/**
 * Source of physical wall-clock time in milliseconds since the Unix epoch.
 * Abstracted so tests can drive deterministic virtual clocks (including
 * deliberate backwards jumps) without touching the real system clock.
 */
public interface PhysicalClock {

    /** Current wall-clock time in milliseconds since the Unix epoch. */
    long millis();
}
