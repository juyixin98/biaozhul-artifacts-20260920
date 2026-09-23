package com.opp16.engine;

import com.opp16.engine.json.Json;

import java.util.Arrays;

/**
 * A columnar field: typed backing array plus a {@link NullBitmap}.
 *
 * Two concrete storages exist:
 * <ul>
 *   <li>{@link IntColumn}    - packed {@code long[]} (64-bit integers)</li>
 *   <li>{@link StringColumn} - object array of {@code String} references</li>
 * </ul>
 * Values for null rows live in the backing arrays too (default 0 / null); only
 * the bitmap decides validity - operators must never read past it.
 */
public abstract class Column {

    public final String name;
    public final ColumnType type;
    protected final NullBitmap nulls;

    protected Column(String name, ColumnType type, NullBitmap nulls) {
        this.name = name;
        this.type = type;
        this.nulls = nulls;
    }

    public final int size() { return nulls.size(); }
    public final NullBitmap nulls() { return nulls; }
    public final boolean isNullAt(int row) { return !nulls.isPresent(row); }

    /** Boxing accessor used by the row-at-a-time interpreter. */
    public abstract Object boxedAt(int row);

    public IntColumn asInt() {
        if (this instanceof IntColumn c) return c;
        throw new EngineException("column '" + name + "' is not INT");
    }

    public StringColumn asString() {
        if (this instanceof StringColumn c) return c;
        throw new EngineException("column '" + name + "' is not STRING");
    }

    /** JSON serialization of the physical data (used by data export). */
    public abstract Json.Value toJson();

    // ------------------------------------------------------------------
    // INT column
    // ------------------------------------------------------------------

    public static final class IntColumn extends Column {
        private final long[] values;

        public IntColumn(String name, long[] values, NullBitmap nulls) {
            super(name, ColumnType.INT, nulls);
            if (values.length != nulls.size()) {
                throw new EngineException("INT column '" + name + "': values/bitmap length mismatch");
            }
            this.values = values;
        }

        public long getLong(int row) { return values[row]; }
        public long[] values() { return values; }

        /** Batch read: bit i of the returned mask means the value at {@code base+i} is non-null. */
        public long presentMask(int base, int len) { return nulls.presentMask(base, len); }

        @Override public Object boxedAt(int row) {
            return nulls.isPresent(row) ? values[row] : null;
        }

        @Override public Json.Value toJson() {
            Json.Obj o = new Json.Obj();
            o.put("name", name);
            o.put("type", "INT");
            Json.Arr arr = new Json.Arr();
            for (int i = 0; i < values.length; i++) {
                arr.add(nulls.isPresent(i) ? new Json.Num(values[i]) : Json.NULL);
            }
            o.put("values", arr);
            return o;
        }
    }

    // ------------------------------------------------------------------
    // STRING column
    // ------------------------------------------------------------------

    public static final class StringColumn extends Column {
        private final String[] values;

        public StringColumn(String name, String[] values, NullBitmap nulls) {
            super(name, ColumnType.STRING, nulls);
            if (values.length != nulls.size()) {
                throw new EngineException("STRING column '" + name + "': values/bitmap length mismatch");
            }
            this.values = values;
        }

        public String getString(int row) { return values[row]; }
        public String[] values() { return values; }

        @Override public Object boxedAt(int row) {
            return nulls.isPresent(row) ? values[row] : null;
        }

        @Override public Json.Value toJson() {
            Json.Obj o = new Json.Obj();
            o.put("name", name);
            o.put("type", "STRING");
            Json.Arr arr = new Json.Arr();
            for (int i = 0; i < values.length; i++) {
                arr.add(nulls.isPresent(i) ? new Json.Str(values[i]) : Json.NULL);
            }
            o.put("values", arr);
            return o;
        }
    }

    // ------------------------------------------------------------------
    // Factory: build a column from a JSON values array (mixed numbers / strings / null)
    // ------------------------------------------------------------------

    public static Column fromJson(String name, String typeName, Json.Arr values) {
        int n = values.size();
        NullBitmap bm = new NullBitmap(n);
        if ("INT".equalsIgnoreCase(typeName)) {
            long[] longs = new long[n];
            for (int i = 0; i < n; i++) {
                Json.Value v = values.get(i);
                if (v.isNull()) continue;
                if (!v.isNumber()) {
                    throw new EngineException("column '" + name + "' row " + i + ": expected INT or null");
                }
                longs[i] = v.asLong();
                bm.setPresent(i);
            }
            return new IntColumn(name, longs, bm);
        }
        if ("STRING".equalsIgnoreCase(typeName)) {
            String[] strs = new String[n];
            for (int i = 0; i < n; i++) {
                Json.Value v = values.get(i);
                if (v.isNull()) continue;
                if (!v.isString()) {
                    throw new EngineException("column '" + name + "' row " + i + ": expected STRING or null");
                }
                strs[i] = v.asString();
                bm.setPresent(i);
            }
            return new StringColumn(name, strs, bm);
        }
        throw new EngineException("unsupported column type: " + typeName + " (INT, STRING supported)");
    }

    /** Test/helper factory for INT columns from boxed values (null allowed). */
    public static IntColumn ints(String name, Long... values) {
        NullBitmap bm = new NullBitmap(values.length);
        long[] raw = new long[values.length];
        for (int i = 0; i < values.length; i++) {
            if (values[i] != null) { raw[i] = values[i]; bm.setPresent(i); }
        }
        return new IntColumn(name, raw, bm);
    }

    /** Test/helper factory for STRING columns from boxed values (null allowed). */
    public static StringColumn strings(String name, String... values) {
        NullBitmap bm = NullBitmap.allPresent(values.length);
        for (int i = 0; i < values.length; i++) if (values[i] == null) bm.setNull(i);
        return new StringColumn(name, Arrays.copyOf(values, values.length), bm);
    }
}
