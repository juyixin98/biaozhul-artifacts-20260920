package com.example.vecsearch;

import java.util.ArrayList;
import java.util.List;

/**
 * Tiny zero-dependency test harness. Tests call {@code check} with a
 * condition and description; failures are collected, printed at the end,
 * and the JVM exits non-zero if anything failed.
 */
public final class TestFramework {

    private int passed;
    private int failed;
    private final List<String> failures = new ArrayList<>();

    public void check(boolean condition, String description) {
        if (condition) {
            passed++;
            System.out.println("  PASS " + description);
        } else {
            failed++;
            failures.add(description);
            System.out.println("  FAIL " + description);
        }
    }

    public void section(String name) {
        System.out.println();
        System.out.println("== " + name + " ==");
    }

    /** Run a block that is expected to throw ApiException with the given status. */
    public void expectApiError(int status, Runnable r, String description) {
        try {
            r.run();
            check(false, description + " (no exception was thrown)");
        } catch (ApiException e) {
            check(e.status() == status,
                    description + " (got status " + e.status() + ", expected " + status
                            + ": " + e.getMessage() + ")");
        }
    }

    public int finish() {
        System.out.println();
        if (failed == 0) {
            System.out.println("RESULT: all " + passed + " checks passed");
            return 0;
        }
        System.out.println("RESULT: " + failed + " of " + (passed + failed) + " checks FAILED");
        for (String f : failures) {
            System.out.println("  - " + f);
        }
        return 1;
    }
}
