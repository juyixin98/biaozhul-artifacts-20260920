package test;

import java.util.ArrayList;
import java.util.List;

/** Minimal test runner: collects pass/fail counts and prints a summary. */
public final class TestRunner {

    private final List<Named> tests = new ArrayList<>();

    private record Named(String name, TestCase body) {
    }

    public TestRunner add(String name, TestCase body) {
        tests.add(new Named(name, body));
        return this;
    }

    public int runAll() {
        int passed = 0;
        List<String> failures = new ArrayList<>();
        for (Named t : tests) {
            try {
                t.body.run();
                System.out.println("PASS  " + t.name);
                passed++;
            } catch (Throwable th) {
                System.out.println("FAIL  " + t.name);
                System.out.println("        " + th);
                failures.add(t.name + ": " + th);
            }
        }
        System.out.println();
        System.out.println("Tests run: " + tests.size() + ", passed: " + passed
                + ", failed: " + failures.size());
        if (!failures.isEmpty()) {
            System.out.println("Failed tests:");
            for (String f : failures) {
                System.out.println("  - " + f);
            }
            return 1;
        }
        return 0;
    }
}
