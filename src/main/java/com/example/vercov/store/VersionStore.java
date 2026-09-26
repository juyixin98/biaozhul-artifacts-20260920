package com.example.vercov.store;

import com.example.vercov.engine.ConflictException;
import com.example.vercov.engine.CoverageEngine;
import com.example.vercov.model.EffectiveSegment;
import com.example.vercov.model.Version;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;

/**
 * In-memory, thread-safe registry of versions. All queries recompute from
 * the current version set, so deleting a version automatically restores
 * whatever lower-priority rules it had shadowed.
 */
public class VersionStore {

    private final Map<String, Version> versions = new LinkedHashMap<>();

    /**
     * Registers a new version.
     *
     * @throws DuplicateVersionException if the id is already registered
     * @throws ConflictException         on a same-priority overlap
     */
    public synchronized Version add(Version version) {
        if (versions.containsKey(version.id())) {
            throw new DuplicateVersionException("version id already exists: " + version.id());
        }
        CoverageEngine.assertNoSamePriorityConflict(versions.values(), version);
        versions.put(version.id(), version);
        return version;
    }

    /** Removes a version; returns true if it existed. */
    public synchronized boolean remove(String id) {
        return versions.remove(id) != null;
    }

    public synchronized Optional<Version> find(String id) {
        return Optional.ofNullable(versions.get(id));
    }

    public synchronized List<Version> list() {
        return List.copyOf(versions.values());
    }

    /** Effective non-overlapping segments over {@code [from, to)}. */
    public synchronized List<EffectiveSegment> coverage(long from, long to) {
        return CoverageEngine.compute(new ArrayList<>(versions.values()), from, to);
    }

    /** The effective segment covering a single point, if any. */
    public synchronized Optional<EffectiveSegment> point(long at) {
        return coverage(at, at + 1).stream().findFirst();
    }

    public static class DuplicateVersionException extends RuntimeException {
        public DuplicateVersionException(String message) {
            super(message);
        }
    }
}
