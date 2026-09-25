package com.example.cptx.core;

/** 真实墙上时钟。 */
final class SystemClock implements Clock {
    @Override
    public long nowMillis() {
        return System.currentTimeMillis();
    }
}
