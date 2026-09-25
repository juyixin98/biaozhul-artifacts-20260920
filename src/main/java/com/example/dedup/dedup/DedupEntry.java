package com.example.dedup.dedup;

/** One retained dedup entry. */
final class DedupEntry {
    final String id;
    long firstEventTime;
    long maxEventTime;
    final String payloadHash; // null = empty payload (e.g. DELETE)
    int insertOrder;

    DedupEntry(String id, long eventTime, String payloadHash, int insertOrder) {
        this.id = id;
        this.firstEventTime = eventTime;
        this.maxEventTime = eventTime;
        this.payloadHash = payloadHash;
        this.insertOrder = insertOrder;
    }
}
