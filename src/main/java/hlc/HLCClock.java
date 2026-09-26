package hlc;

import java.util.Objects;

/**
 * A Hybrid Logical Clock (Kulkarni et al., 2014) for a single node.
 *
 * <p>The clock holds the latest issued timestamp {@code (l, c)} and is driven by an
 * injectable {@link PhysicalClock} that supplies physical time in microseconds. It supports
 * the three required operations:
 *
 * <ul>
 *   <li><b>local/send</b> &mdash; {@link #tickLocal()}/{@link #send()}: observe a local
 *       event or attach a timestamp to an outgoing message;</li>
 *   <li><b>receive/merge</b> &mdash; {@link #receive(HLCTimestamp)}: absorb the timestamp
 *       of an incoming message;</li>
 *   <li><b>persistence &amp; recovery</b> &mdash; {@link #snapshot()}/{@link #restore(HLCTimestamp)}.</li>
 * </ul>
 *
 * <p><b>Clock regression.</b> The physical clock is treated as advisory. When it jumps
 * backwards (NTP step, virtual-clock reset, live migration), {@code l} simply holds at its
 * previous value and {@code c} advances, so issued timestamps stay strictly monotonic.
 *
 * <p><b>Counter overflow.</b> The counter is bounded by {@link LogicalCounterOverflowException#MAX_COUNTER}.
 * If the physical clock cannot move past {@code l} and the counter is exhausted, the next
 * operation throws {@link LogicalCounterOverflowException} rather than wrapping or
 * saturating; no out-of-order timestamp is ever produced.
 *
 * <p>This class is thread-safe: every mutating operation is serialized on the instance.
 */
public final class HLCClock {

    private final PhysicalClock physical;
    private volatile HLCTimestamp current;

    public HLCClock(PhysicalClock physical) {
        this(physical, HLCTimestamp.ZERO);
    }

    public HLCClock(PhysicalClock physical, HLCTimestamp initial) {
        this.physical = Objects.requireNonNull(physical, "physical clock");
        this.current = Objects.requireNonNull(initial, "initial timestamp");
    }

    /** Current timestamp without advancing the clock. */
    public HLCTimestamp peek() {
        return current;
    }

    /**
     * Local event (also the algorithm used when <em>sending</em> a message), per the HLC
     * paper (Kulkarni et al., 2014):
     * <pre>
     *   l' = max(l, pt.now)
     *   if l' == l then c' = c + 1   // physical time did not advance past l
     *   else           c' = 0        // physical time advanced; counter resets
     * </pre>
     */
    public synchronized HLCTimestamp tickLocal() {
        long pt = physical.nowMicros();
        long oldL = current.l();
        long newL = Math.max(oldL, pt);
        long newC = (newL == oldL) ? increment(current.c(), "local event") : 0L;
        current = new HLCTimestamp(newL, newC);
        return current;
    }

    /** Convenience alias: timestamp an outgoing message. */
    public HLCTimestamp send() {
        return tickLocal();
    }

    /**
     * Receive/merge event for an incoming message timestamp {@code (lm, cm)}, per the HLC
     * paper:
     * <pre>
     *   l' = max(l, lm, pt.now)
     *   if l' == l or l' == lm:
     *       c' = 1 + max( c   if l' == l   else -1,
     *                     cm  if l' == lm  else -1 )
     *   else:                 // physical time strictly ahead of both logical times
     *       c' = 0
     * </pre>
     * This guarantees the new timestamp is strictly greater than both the previous local
     * timestamp and the received timestamp whenever logical times tie, while a strictly
     * advancing physical clock resets the counter to 0. Timestamps stay ordered even while
     * the local physical clock is behind.
     */
    public synchronized HLCTimestamp receive(HLCTimestamp message) {
        Objects.requireNonNull(message, "received timestamp");
        long pt = physical.nowMicros();
        long oldL = current.l();
        long oldC = current.c();
        long ml = message.l();
        long mc = message.c();

        long newL = Math.max(Math.max(oldL, ml), pt);

        long winningC = -1L;
        if (newL == oldL) {
            winningC = Math.max(winningC, oldC);
        }
        if (newL == ml) {
            winningC = Math.max(winningC, mc);
        }
        long newC = increment(winningC, "receive with l=" + newL);

        current = new HLCTimestamp(newL, newC);
        return current;
    }

    private static long increment(long c, String context) {
        if (c >= LogicalCounterOverflowException.MAX_COUNTER) {
            throw new LogicalCounterOverflowException(
                    "HLC logical counter reached " + LogicalCounterOverflowException.MAX_COUNTER
                            + " during '" + context + "'; waiting for physical time to advance past l "
                            + "is the only safe recovery");
        }
        return c + 1L;
    }

    /** Immutable capture of the clock state for durable storage. */
    public HLCTimestamp snapshot() {
        return current;
    }

    /**
     * Rebuild clock state after restart. The physical clock may have moved in either
     * direction; later operations reconcile it via the normal merge rules, so restoring a
     * state ahead of the wall clock is safe (and required for monotonicity across restarts).
     */
    public synchronized void restore(HLCTimestamp saved) {
        this.current = Objects.requireNonNull(saved, "saved timestamp");
    }

    /**
     * Returns the tzdata version of the running JVM (informational metadata; HLC itself is
     * timezone-independent). See {@link TimeZoneInfo}.
     */
    public static String timeZoneDatabaseVersion() {
        return TimeZoneInfo.version();
    }
}
