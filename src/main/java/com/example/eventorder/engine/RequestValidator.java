package com.example.eventorder.engine;

import com.example.eventorder.model.DependencySpec;
import com.example.eventorder.model.EventSpec;
import com.example.eventorder.model.OrderOptions;
import com.example.eventorder.model.OrderRequest;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.format.DateTimeParseException;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeMap;

/**
 * Turns a raw {@link OrderRequest} into a {@link ValidatedInput} or fails with a
 * {@link RequestException} describing the first problem found.
 */
public final class RequestValidator {

    static final boolean DEFAULT_ENFORCE_VERSION_ORDER = false;
    static final int DEFAULT_MAX_ENUMERATED_ORDERS = 100;
    static final int MAX_ENUMERATED_ORDERS_LIMIT = 10_000;

    private RequestValidator() {
    }

    public static ValidatedInput validate(OrderRequest request) {
        if (request == null) {
            throw new RequestException("request body must not be null");
        }
        Map<String, ValidatedEvent> events = validateEvents(request.events());
        List<DependencySpec> dependencies = validateDependencies(request.dependencies(), events.keySet());
        OrderOptions options = request.options() == null
                ? new OrderOptions(null, null) : request.options();
        boolean enforceVersionOrder = options.enforceVersionOrder() == null
                ? DEFAULT_ENFORCE_VERSION_ORDER : options.enforceVersionOrder();
        int maxEnumeratedOrders = options.maxEnumeratedOrders() == null
                ? DEFAULT_MAX_ENUMERATED_ORDERS : options.maxEnumeratedOrders();
        if (maxEnumeratedOrders < 0 || maxEnumeratedOrders > MAX_ENUMERATED_ORDERS_LIMIT) {
            throw new RequestException("options.maxEnumeratedOrders must be between 0 and "
                    + MAX_ENUMERATED_ORDERS_LIMIT);
        }
        return new ValidatedInput(request.requestId(), events, dependencies,
                enforceVersionOrder, maxEnumeratedOrders);
    }

    private static Map<String, ValidatedEvent> validateEvents(List<EventSpec> specs) {
        if (specs == null || specs.isEmpty()) {
            throw new RequestException("events must contain at least one event");
        }
        Map<String, ValidatedEvent> events = new TreeMap<>();
        for (EventSpec spec : specs) {
            if (spec == null || spec.id() == null || spec.id().isBlank()) {
                throw new RequestException("every event needs a non-blank id");
            }
            String id = spec.id();
            if (events.containsKey(id)) {
                throw new RequestException("duplicate event id: " + id);
            }
            Optional<Instant> earliest = parseInstant(spec.earliest(), id, "earliest");
            Optional<Instant> latest = parseInstant(spec.latest(), id, "latest");
            events.put(id, new ValidatedEvent(id, earliest, latest, spec.version()));
        }
        return events;
    }

    private static Optional<Instant> parseInstant(String raw, String eventId, String field) {
        if (raw == null) {
            return Optional.empty();
        }
        try {
            // OffsetDateTime accepts both "...Z" and "+08:00" style offsets; a bare
            // local date-time without offset is rejected on purpose (determinism).
            return Optional.of(OffsetDateTime.parse(raw).toInstant());
        } catch (DateTimeParseException e) {
            throw new RequestException("event '" + eventId + "' has unparseable " + field
                    + " '" + raw + "'; use ISO-8601 with offset, e.g. 2026-01-01T09:00:00Z");
        }
    }

    private static List<DependencySpec> validateDependencies(List<DependencySpec> specs,
                                                             Set<String> eventIds) {
        if (specs == null) {
            return List.of();
        }
        List<DependencySpec> result = new ArrayList<>();
        Set<String> seen = new HashSet<>();
        for (DependencySpec spec : specs) {
            if (spec == null || spec.before() == null || spec.after() == null) {
                throw new RequestException("every dependency needs 'before' and 'after'");
            }
            if (!eventIds.contains(spec.before())) {
                throw new RequestException("dependency references unknown event: " + spec.before());
            }
            if (!eventIds.contains(spec.after())) {
                throw new RequestException("dependency references unknown event: " + spec.after());
            }
            if (!seen.add(spec.before() + " -> " + spec.after())) {
                throw new RequestException("duplicate dependency: "
                        + spec.before() + " -> " + spec.after());
            }
            result.add(spec);
        }
        return result;
    }
}
