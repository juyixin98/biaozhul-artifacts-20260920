package com.example.vic.domain;

/**
 * A rule version. Priority decides overlay order: higher priority wins on overlap.
 * Two versions with the same priority must never have overlapping rules; that is a conflict.
 */
public record VersionDef(String id, int priority) {

    public VersionDef {
        if (id == null || id.isBlank()) {
            throw new IllegalArgumentException("version id must not be blank");
        }
    }
}
