package com.example.segindex;

/**
 * One occurrence of a term in a document.
 *
 * @param docId      external document id (may be reused across generations)
 * @param generation monotonically increasing per docId, distinguishes reuse of the same id
 * @param freq       number of times the term occurs in this document
 */
public record Posting(String docId, long generation, int freq) {
}
