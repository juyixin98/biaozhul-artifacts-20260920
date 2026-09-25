package com.eventorder.model;

/**
 * A directed ordering edge: {@code from} must be ordered before {@code to}.
 *
 * @param kind   which rule produced this edge
 * @param reason human-readable justification, used in conflict chains
 */
public record Edge(String from, String to, Kind kind, String reason) {

    public enum Kind {
        EXPLICIT,
        TIME,
        VERSION
    }
}
