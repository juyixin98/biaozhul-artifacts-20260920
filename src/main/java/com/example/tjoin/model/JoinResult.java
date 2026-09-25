package com.example.tjoin.model;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import com.fasterxml.jackson.annotation.JsonPropertyOrder;

/**
 * One emitted join result: an (left, right) event pair whose event times fall
 * inside the configured interval and whose keys are equal.
 *
 * <p>The pair {@code (leftId, rightId)} is unique across the whole output —
 * the operator emits any given pair at most once, even under redelivery.</p>
 */
@JsonInclude(JsonInclude.Include.NON_NULL)
@JsonPropertyOrder({"leftId", "rightId", "key", "leftEventTime", "rightEventTime",
        "timeDistance", "leftValue", "rightValue", "emittedAtProcessingTime"})
public final class JoinResult {

    private final String leftId;
    private final String rightId;
    private final String key;
    private final long leftTimestamp;
    private final long rightTimestamp;
    private final Object leftValue;
    private final Object rightValue;
    private final long emittedAtProcessingTime;

    public JoinResult(StreamEvent left, StreamEvent right, long emittedAtProcessingTime) {
        this.leftId = left.getId();
        this.rightId = right.getId();
        this.key = left.getKey();
        this.leftTimestamp = left.getTimestamp();
        this.rightTimestamp = right.getTimestamp();
        this.leftValue = left.getValue();
        this.rightValue = right.getValue();
        this.emittedAtProcessingTime = emittedAtProcessingTime;
    }

    public String getLeftId() {
        return leftId;
    }

    public String getRightId() {
        return rightId;
    }

    public String getKey() {
        return key;
    }

    @JsonProperty("leftEventTime")
    public long getLeftTimestamp() {
        return leftTimestamp;
    }

    @JsonProperty("rightEventTime")
    public long getRightTimestamp() {
        return rightTimestamp;
    }

    /** {@code rightEventTime - leftEventTime}; always within {@code [lowerBound, upperBound]}. */
    public long getTimeDistance() {
        return rightTimestamp - leftTimestamp;
    }

    public Object getLeftValue() {
        return leftValue;
    }

    public Object getRightValue() {
        return rightValue;
    }

    public long getEmittedAtProcessingTime() {
        return emittedAtProcessingTime;
    }
}
