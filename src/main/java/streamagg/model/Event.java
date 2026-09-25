package streamagg.model;

import java.math.BigDecimal;

/** Current state of a live event. Retracted events leave no {@code Event} behind. */
public record Event(
        String eventId,
        String key,
        BigDecimal value,
        long version,
        Long createdAt,
        Long updatedAt) {
}
