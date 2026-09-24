package com.example.positiondiff.diff;

/**
 * Limits the work Myers may spend before it is allowed to give up.
 *
 * <p>{@code maxNodes} bounds the number of V-array slots updated during the
 * forward search (one unit charged per snake endpoint reached); {@code maxD}
 * bounds the edit distance the search explores. Any value {@code < 0} means
 * unlimited. When a limit is hit the engine returns an explicit
 * <em>degraded</em> (always valid, not claimed shortest) result.
 */
public final class Budget {

    public static final long UNLIMITED = -1L;

    public static final Budget UNLIMITED_BUDGET = new Budget(UNLIMITED, UNLIMITED);

    private final long maxNodes;
    private final long maxD;

    public Budget(long maxNodes, long maxD) {
        this.maxNodes = maxNodes;
        this.maxD = maxD;
    }

    public static Budget maxNodes(long maxNodes) {
        return new Budget(maxNodes, UNLIMITED);
    }

    public static Budget maxD(long maxD) {
        return new Budget(UNLIMITED, maxD);
    }

    public long maxNodes() { return maxNodes; }
    public long maxD() { return maxD; }

    public boolean nodesUnlimited() { return maxNodes < 0; }
    public boolean dUnlimited() { return maxD < 0; }
}
