package com.example.eventorder.engine;

import java.time.Instant;
import java.util.Optional;

/**
 * Validated, engine-ready view of one event.
 *
 * @param id       event id
 * @param earliest parsed window start, empty when unspecified
 * @param latest   parsed window end, empty when unspecified
 * @param version  optional logical version
 */
public record ValidatedEvent(String id,
                             Optional<Instant> earliest,
                             Optional<Instant> latest,
                             Long version) {

    public boolean hasWindow() {
        return earliest.isPresent() || latest.isPresent();
    }
}
