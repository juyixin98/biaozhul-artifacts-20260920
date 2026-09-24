package com.tvl.core;

import com.tvl.columnar.ColumnVector;

import java.util.Arrays;

/**
 * 三值布尔向量：每个字节是 Ternary.code（0/1/2）。
 * WHERE 的选择位图 = code == TRUE.code，UNKNOWN 与 FALSE 一样被过滤。
 */
public final class TruthVector {

    private final byte[] codes;

    public TruthVector(byte[] codes) {
        this.codes = codes;
    }

    public static TruthVector ofSize(int size) {
        return new TruthVector(new byte[size]);
    }

    public int size() {
        return codes.length;
    }

    public byte[] codes() {
        return codes;
    }

    public byte codeAt(int i) {
        return codes[i];
    }

    public Ternary at(int i) {
        return Ternary.ofCode(codes[i]);
    }

    /** WHERE 放行位图。 */
    public boolean[] selectedBitmap() {
        boolean[] out = new boolean[codes.length];
        for (int i = 0; i < codes.length; i++) {
            out[i] = codes[i] == Ternary.TRUE.code;
        }
        return out;
    }

    public long countSelected() {
        long n = 0;
        for (byte c : codes) {
            if (c == Ternary.TRUE.code) {
                n++;
            }
        }
        return n;
    }

    public TruthVector not() {
        byte[] out = new byte[codes.length];
        for (int i = 0; i < codes.length; i++) {
            out[i] = Ternary.notCode(codes[i]);
        }
        return new TruthVector(out);
    }

    public TruthVector and(TruthVector other) {
        byte[] out = new byte[codes.length];
        for (int i = 0; i < codes.length; i++) {
            out[i] = Ternary.andCode(codes[i], other.codes[i]);
        }
        return new TruthVector(out);
    }

    public TruthVector or(TruthVector other) {
        byte[] out = new byte[codes.length];
        for (int i = 0; i < codes.length; i++) {
            out[i] = Ternary.orCode(codes[i], other.codes[i]);
        }
        return new TruthVector(out);
    }

    @Override
    public boolean equals(Object o) {
        return (o instanceof TruthVector) && Arrays.equals(codes, ((TruthVector) o).codes);
    }

    @Override
    public int hashCode() {
        return Arrays.hashCode(codes);
    }

    @Override
    public String toString() {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < codes.length; i++) {
            if (i > 0) {
                sb.append(", ");
            }
            sb.append(Ternary.ofCode(codes[i]).name());
        }
        return sb.append(']').toString();
    }
}
