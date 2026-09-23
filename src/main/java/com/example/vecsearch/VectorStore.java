package com.example.vecsearch;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * In-memory vector store.
 *
 * Each id maps to one {@link Entry} holding its (dense, fixed-dimension)
 * vector, precomputed L2 norm and arbitrary JSON metadata.
 * Deletes are tombstones: the slot is removed from the live map entirely.
 * Upsert of an existing id replaces its vector and metadata (after the
 * same dimension and zero-vector validation as a fresh insert).
 *
 * All methods are synchronized; the service serializes mutating and
 * searching operations on this lock.
 */
public class VectorStore {

    public static final class Entry {
        final String id;
        final double[] vector;
        final double norm;
        final Map<String, Object> metadata;

        Entry(String id, double[] vector, double norm, Map<String, Object> metadata) {
            this.id = id;
            this.vector = vector;
            this.norm = norm;
            this.metadata = metadata;
        }
    }

    private final Map<String, Entry> entries = new HashMap<>();
    private int dimension = -1; // -1 = empty store, dimension not fixed yet

    public synchronized int dimension() {
        return dimension;
    }

    public synchronized int size() {
        return entries.size();
    }

    public synchronized boolean contains(String id) {
        return entries.containsKey(id);
    }

    public synchronized Entry get(String id) {
        return entries.get(id);
    }

    /**
     * Insert or replace a vector.
     *
     * @return true if this replaced an existing (live) id
     * @throws ApiException on dimension mismatch or zero vector for cosine
     */
    public synchronized boolean upsert(String id, double[] vector, double norm,
                                       Map<String, Object> metadata, Metric metric) {
        if (dimension < 0) {
            dimension = vector.length;
        } else if (vector.length != dimension) {
            throw new ApiException(400,
                    "dimension mismatch: expected " + dimension + " but got " + vector.length);
        }
        if (metric == Metric.COSINE && norm == 0.0) {
            throw new ApiException(400,
                    "zero vector is not allowed under COSINE (cosine distance is undefined)");
        }
        boolean replaced = entries.containsKey(id);
        entries.put(id, new Entry(id, vector, norm, metadata));
        return replaced;
    }

    /** Delete an id. @return true if a live entry was removed. */
    public synchronized boolean delete(String id) {
        return entries.remove(id) != null;
    }

    /** Snapshot of all live entries (safe to iterate without the lock). */
    public synchronized List<Entry> snapshot() {
        return new ArrayList<>(entries.values());
    }

    public synchronized void clear() {
        entries.clear();
        dimension = -1;
    }
}
