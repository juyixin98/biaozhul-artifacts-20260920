package com.example.tjoin;

import com.example.tjoin.model.BufferOverflowPolicy;
import com.example.tjoin.model.JoinConfig;
import com.example.tjoin.model.StreamEvent;

/** Shared test helpers. */
final class TestUtil {

    private TestUtil() {
    }

    static StreamEvent e(String id, String key, long ts) {
        return new StreamEvent(id, key, ts, null);
    }

    static StreamEvent e(String id, String key, long ts, Object value) {
        return new StreamEvent(id, key, ts, value);
    }

    /** Config with zero out-of-orderness, no idleness, unlimited buffers. */
    static JoinConfig config(long lower, long upper) {
        return JoinConfig.symmetric(lower, upper, 0, 0, 0, BufferOverflowPolicy.REJECT);
    }

    static JoinConfig config(long lower, long upper,
                             long leftOoo, long rightOoo) {
        return new JoinConfig(lower, upper,
                new JoinConfig.SideConfig(leftOoo, 0, 0, BufferOverflowPolicy.REJECT),
                new JoinConfig.SideConfig(rightOoo, 0, 0, BufferOverflowPolicy.REJECT));
    }

    static JoinConfig configWithIdle(long lower, long upper,
                                     long ooo, long idleTimeout) {
        return JoinConfig.symmetric(lower, upper, ooo, idleTimeout, 0,
                BufferOverflowPolicy.REJECT);
    }

    static JoinConfig configWithCap(long lower, long upper, int leftCap, int rightCap,
                                    BufferOverflowPolicy policy) {
        return new JoinConfig(lower, upper,
                new JoinConfig.SideConfig(0, 0, leftCap, policy),
                new JoinConfig.SideConfig(0, 0, rightCap, policy));
    }
}
