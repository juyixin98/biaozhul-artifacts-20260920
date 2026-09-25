package com.example.dedup.window;

import com.example.dedup.json.Json;

/** Per-key accumulator inside one window. */
final class KeyAcc {
    String lastUpsertId;
    Json.Value lastPayload;
    long upsertCount;
    long deleteCount;
    long lastUpsertTime = Long.MIN_VALUE;
    long lastDeleteTime = Long.MIN_VALUE;
}
