package hlc;

/**
 * Physical clock source, returning physical time in <b>microseconds</b> since the Unix epoch.
 *
 * <p>Implementations may read the wall clock or, for deterministic testing, return a
 * scripted / virtual value. The HLC algorithm treats this value as untrusted: it must be
 * monotonic with respect to the returned HLC timestamps even if the underlying clock
 * leaps backwards (see {@link HLCClock}).
 */
@FunctionalInterface
public interface PhysicalClock {

    long nowMicros();

    /** Wall-clock source: {@link System#currentTimeMillis()} expressed in microseconds. */
    static PhysicalClock systemDefault() {
        return () -> System.currentTimeMillis() * 1000L;
    }

    /** Deterministic source that always returns the same physical time. */
    static PhysicalClock fixed(long micros) {
        return () -> micros;
    }

    /** Deterministic source whose time can be moved arbitrarily, including backwards. */
    static VirtualClock virtual(long initialMicros) {
        return new VirtualClock(initialMicros);
    }

    /** Mutable physical clock used by tests and by fixed-data scenarios. */
    final class VirtualClock implements PhysicalClock {
        private volatile long micros;

        public VirtualClock(long initialMicros) {
            this.micros = initialMicros;
        }

        @Override
        public long nowMicros() {
            return micros;
        }

        public void set(long micros) {
            this.micros = micros;
        }
    }
}
