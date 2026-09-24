package com.example.segindex;

import java.util.function.Consumer;

/** Tunables for an {@link Index}. */
public class IndexConfig {

    /** Merge is triggered (in the background) when live segment count reaches this. */
    private final int mergeFactor;
    /** Whether the background merge thread runs. */
    private final boolean backgroundMergeEnabled;
    /**
     * Test hook invoked at named crash points (e.g. "commit.beforeManifestCommit").
     * Throwing from the hook simulates a process crash. No-op by default.
     */
    private final Consumer<String> crashHook;

    private IndexConfig(Builder b) {
        this.mergeFactor = b.mergeFactor;
        this.backgroundMergeEnabled = b.backgroundMergeEnabled;
        this.crashHook = b.crashHook;
    }

    public static IndexConfig defaults() {
        return new Builder().build();
    }

    public static Builder builder() {
        return new Builder();
    }

    public int mergeFactor() {
        return mergeFactor;
    }

    public boolean backgroundMergeEnabled() {
        return backgroundMergeEnabled;
    }

    public Consumer<String> crashHook() {
        return crashHook;
    }

    public static final class Builder {
        private int mergeFactor = 4;
        private boolean backgroundMergeEnabled = true;
        private Consumer<String> crashHook = point -> {
        };

        public Builder mergeFactor(int mergeFactor) {
            if (mergeFactor < 2) {
                throw new IllegalArgumentException("mergeFactor must be >= 2");
            }
            this.mergeFactor = mergeFactor;
            return this;
        }

        public Builder backgroundMergeEnabled(boolean enabled) {
            this.backgroundMergeEnabled = enabled;
            return this;
        }

        public Builder crashHook(Consumer<String> hook) {
            this.crashHook = hook;
            return this;
        }

        public IndexConfig build() {
            return new IndexConfig(this);
        }
    }
}
