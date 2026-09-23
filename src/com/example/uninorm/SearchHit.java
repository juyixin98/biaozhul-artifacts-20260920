package com.example.uninorm;

/** One search hit: the document and the matched span in ORIGINAL offsets. */
public record SearchHit(String docId, OffsetRange range, String matchedOriginal) {
}
