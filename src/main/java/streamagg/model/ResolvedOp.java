package streamagg.model;

/**
 * One lifecycle change at the moment it actually resolved, in global resolution order.
 * This is what the event ledger stores and what the reference replayer consumes.
 *
 * @param oldKey   key the event belonged to before the change (null for ADD)
 * @param oldValue value before the change (null for ADD)
 * @param newKey   key after the change (null for RETRACT)
 * @param newValue value after the change (null for RETRACT)
 */
public record ResolvedOp(
        String opId,
        String eventId,
        OpType op,
        long version,
        String oldKey,
        java.math.BigDecimal oldValue,
        String newKey,
        java.math.BigDecimal newValue,
        Long eventTime,
        Long resolvedTime) {
}
