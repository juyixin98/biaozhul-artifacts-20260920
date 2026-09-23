package partjoin.test;

import partjoin.Row;
import partjoin.Schema;
import partjoin.Table;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/** Shared data builders for the join tests. */
final class Data {

    private Data() {
    }

    static final Schema L2 = Schema.of("id", "lk");
    static final Schema R2 = Schema.of("rid", "rk", "rv");

    static Table table(String name, Schema schema, List<Row> rows) {
        return new Table(name, schema, rows);
    }

    static Row row(Object... values) {
        return Row.of(values);
    }

    /**
     * Randomized table pair used for multiset cross-checks.
     * Keys are drawn from a small domain (producing duplicates and a hot key),
     * with a NULL injection rate. A deterministic payload column distinguishes
     * rows that share a key.
     */
    static TablePair randomPair(long seed, int leftN, int rightN, int keyDomain,
                                double nullRate, int payloadWidth) {
        Random rnd = new Random(seed);
        List<Row> ls = new ArrayList<>();
        List<Row> rs = new ArrayList<>();
        for (int i = 0; i < leftN; i++) {
            Object[] vals = new Object[2 + payloadWidth];
            vals[0] = i;
            vals[1] = maybeKey(rnd, keyDomain, nullRate);
            for (int p = 0; p < payloadWidth; p++) vals[2 + p] = "L" + i + "p" + p;
            ls.add(Row.of(vals));
        }
        for (int i = 0; i < rightN; i++) {
            Object[] vals = new Object[3 + payloadWidth];
            vals[0] = i;
            vals[1] = maybeKey(rnd, keyDomain, nullRate);
            vals[2] = "rv" + i;
            for (int p = 0; p < payloadWidth; p++) vals[3 + p] = "R" + i + "p" + p;
            rs.add(Row.of(vals));
        }
        List<String> lCols = new ArrayList<>(List.of("id", "lk"));
        List<String> rCols = new ArrayList<>(List.of("rid", "rk", "rv"));
        for (int p = 0; p < payloadWidth; p++) {
            lCols.add("lp" + p);
            rCols.add("rp" + p);
        }
        return new TablePair(
                new Table("l", new Schema(lCols), ls),
                new Table("r", new Schema(rCols), rs));
    }

    private static Object maybeKey(Random rnd, int domain, double nullRate) {
        if (rnd.nextDouble() < nullRate) return null;
        return rnd.nextInt(domain);
    }

    /** All rows carry the same non-null key, each with a unique payload. */
    static TablePair allHot(int leftN, int rightN) {
        Random rnd = new Random(42);
        List<Row> ls = new ArrayList<>();
        List<Row> rs = new ArrayList<>();
        for (int i = 0; i < leftN; i++) {
            ls.add(Row.of(i, 7, "Lpayload-" + i + "-" + rnd.nextInt(1_000_000)));
        }
        for (int i = 0; i < rightN; i++) {
            rs.add(Row.of(100_000 + i, 7, "Rpayload-" + i + "-" + rnd.nextInt(1_000_000)));
        }
        return new TablePair(
                new Table("l", Schema.of("id", "lk", "lp"), ls),
                new Table("r", Schema.of("rid", "rk", "rv"), rs));
    }

    /** Mostly uniform keys plus one heavily skewed hot key. */
    static TablePair mixedSkew(long seed, int leftN, int rightN, int uniformDomain) {
        Random rnd = new Random(seed);
        List<Row> ls = new ArrayList<>();
        List<Row> rs = new ArrayList<>();
        for (int i = 0; i < leftN; i++) {
            int k = i % 4 == 0 ? 7 : 1000 + rnd.nextInt(uniformDomain);
            ls.add(Row.of(i, k, "L" + i));
        }
        for (int i = 0; i < rightN; i++) {
            int k = i % 5 == 0 ? 7 : 1000 + rnd.nextInt(uniformDomain);
            rs.add(Row.of(i, k, "R" + i));
        }
        return new TablePair(
                new Table("l", Schema.of("id", "lk", "lp"), ls),
                new Table("r", Schema.of("rid", "rk", "rv"), rs));
    }

    record TablePair(Table left, Table right) {
    }
}
