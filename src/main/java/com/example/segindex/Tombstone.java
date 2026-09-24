package com.example.segindex;

/**
 * Delete marker: invalidates every posting of {@code docId} whose generation
 * is {@code <= maxGeneration}. Later re-adds of the same id get a higher
 * generation and are therefore unaffected.
 */
public record Tombstone(String docId, long maxGeneration) {
}
