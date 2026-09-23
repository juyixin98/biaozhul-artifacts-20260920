package com.example.stablepager;

/** Demo rows. Scores intentionally repeat to exercise the {@code (score, id)} tie-break. */
public final class SeedData {

    private SeedData() {
    }

    public static void seed(MvccStore store) {
        // 23 rows: score 50 repeats five times, 20 twice; categories repeat too.
        Object[][] rows = {
                {"a-001", "Alpha", "books", 10L},
                {"a-002", "Bravo", "music", 20L},
                {"a-003", "Charlie", "books", 30L},
                {"a-004", "Delta", "games", 40L},
                {"a-005", "Echo", "music", 50L},
                {"a-006", "Foxtrot", "books", 50L},
                {"a-007", "Golf", "games", 50L},
                {"a-008", "Hotel", "music", 50L},
                {"a-009", "India", "books", 50L},
                {"a-010", "Juliet", "games", 60L},
                {"a-011", "Kilo", "music", 70L},
                {"a-012", "Lima", "books", 80L},
                {"a-013", "Mike", "games", 20L},
                {"a-014", "November", "music", 90L},
                {"a-015", "Oscar", "books", 100L},
                {"a-016", "Papa", "games", 110L},
                {"a-017", "Quebec", "music", 120L},
                {"a-018", "Romeo", "books", 130L},
                {"a-019", "Sierra", "games", 140L},
                {"a-020", "Tango", "music", 150L},
                {"a-021", "Uniform", "books", 5L},
                {"a-022", "Victor", "games", 160L},
                {"a-023", "Whiskey", "music", 170L},
        };
        for (Object[] r : rows) {
            store.insert((String) r[0], (String) r[1], (String) r[2], (Long) r[3]);
        }
    }
}
