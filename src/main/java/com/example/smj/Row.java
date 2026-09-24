package com.example.smj;

import java.util.Comparator;

/**
 * One input row: a 64-bit integer join key plus the row's original JSON text.
 *
 * <p>{@code weight} is the in-memory accounting unit used against the memory budget:
 * the UTF-8 length of the JSON text plus a fixed overhead allowance.
 */
public record Row(long key, String json, int weight) {

  public static final Comparator<Row> BY_KEY = Comparator.comparingLong(Row::key);

  /** Fixed per-row overhead allowance (object headers, references), in bytes. */
  private static final int ROW_OVERHEAD = 16;

  public static Row of(long key, String json) {
    return new Row(key, json, weightOf(json));
  }

  public static int weightOf(String json) {
    return utf8Length(json) + ROW_OVERHEAD;
  }

  /** UTF-8 length without allocating a byte array. */
  static int utf8Length(String s) {
    int len = 0;
    for (int i = 0; i < s.length(); i++) {
      char c = s.charAt(i);
      if (c < 0x80) {
        len += 1;
      } else if (c < 0x800) {
        len += 2;
      } else if (Character.isHighSurrogate(c) && i + 1 < s.length()
          && Character.isLowSurrogate(s.charAt(i + 1))) {
        len += 4;
        i++;
      } else {
        len += 3;
      }
    }
    return len;
  }
}
