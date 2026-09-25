package streamagg.model;

import java.math.BigDecimal;

/**
 * One raw lifecycle operation arriving on the stream.
 *
 * <ul>
 *   <li>{@code eventId}: required; operations are keyed by it.</li>
 *   <li>{@code op}: ADD / RETRACT / CORRECT.</li>
 *   <li>{@code key}: grouping key (required for ADD; CORRECT may change it).</li>
 *   <li>{@code value}: metric (required for ADD; CORRECT may change it).</li>
 *   <li>{@code version}: optional 1-based sequence per event ID. When present, the
 *       engine enforces in-order resolution (1,2,3,...) and buffers out-of-order
 *       operations until the gap fills. When absent, operations resolve in
 *       arrival order, and a RETRACT/CORRECT for an unknown event is buffered
 *       until an ADD arrives.</li>
 *   <li>{@code opId}: client-supplied dedupe token. Replays with the same
 *       {@code opId} are reported {@link IngestStatus#DUPLICATE} and never
 *       applied twice. When absent, an idempotency fingerprint over
 *       (eventId, op, version, key, value) suppresses identical replays.</li>
 *   <li>{@code eventTime}: event-time epoch millis (nullable; watermark use).</li>
 * </ul>
 */
public record IngestOp(
        String opId,
        String eventId,
        OpType op,
        String key,
        BigDecimal value,
        Long version,
        Long eventTime) {

    public IngestOp withVersion(long v) {
        return new IngestOp(opId, eventId, op, key, value, v, eventTime);
    }
}
