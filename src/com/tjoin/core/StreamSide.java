package com.tjoin.core;

/**
 * 双流中的一侧。
 */
public enum StreamSide {
    LEFT,
    RIGHT;

    public StreamSide opposite() {
        return this == LEFT ? RIGHT : LEFT;
    }
}
