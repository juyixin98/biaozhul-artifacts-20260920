package com.example.intervals.engine;

import com.example.intervals.error.ErrorCode;
import com.example.intervals.error.IntervalException;

import java.util.Map;
import java.util.Set;

/**
 * Registry of the domains known to the service. Lookup by the JSON
 * {@code domain} token is case-sensitive and rejects unknown values.
 */
public final class Domains {

    private static final Map<String, Domain<?>> BY_ID = Map.of(
            TimeDomain.ID, new TimeDomain(),
            VersionDomain.ID, new VersionDomain());

    private Domains() {
    }

    public static Set<String> ids() {
        return BY_ID.keySet();
    }

    @SuppressWarnings("unchecked")
    public static <T extends Comparable<? super T>> Domain<T> require(String id) {
        if (id == null || id.isBlank()) {
            throw new IntervalException(ErrorCode.UNKNOWN_DOMAIN,
                    "missing 'domain'; expected one of " + BY_ID.keySet());
        }
        Domain<?> domain = BY_ID.get(id);
        if (domain == null) {
            throw new IntervalException(ErrorCode.UNKNOWN_DOMAIN,
                    "unknown domain '" + id + "'; expected one of " + BY_ID.keySet());
        }
        return (Domain<T>) domain;
    }
}
