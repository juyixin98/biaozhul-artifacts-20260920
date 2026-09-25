package com.timeconv.json;

/** A JSON number preserved as its raw source text — never converted to a float. */
public record JsonNumber(String raw) {

    @Override
    public String toString() {
        return raw;
    }
}
