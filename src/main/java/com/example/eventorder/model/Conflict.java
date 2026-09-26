package com.example.eventorder.model;

import java.util.List;

/**
 * One unsatisfiable core: a minimal readable conflict chain.
 *
 * @param kind   "CYCLE" or "TIME_CONTRADICTION" or "VERSION_CONTRADICTION"
 * @param chain  ordered event ids forming the conflict; for a cycle the first id
 *               repeats at the end (A -&gt; B -&gt; A renders as [A, B, A])
 * @param detail human-readable explanation of why this chain is unsatisfiable
 */
public record Conflict(String kind, List<String> chain, String detail) {
}
