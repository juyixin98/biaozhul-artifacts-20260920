package com.example.smj;

import java.util.Map;

/**
 * Parses one JSONL line into a {@link Row}.
 *
 * <p>Key rules: the key field must be present; a JSON {@code null} key yields {@code null}
 * (the row never joins and is dropped); any other non-integer key (string, float, boolean,
 * out-of-range) is a data error.
 */
public final class RowParser {

  private RowParser() {}

  /** @return the parsed row, or {@code null} when the key is JSON null. */
  @SuppressWarnings("unchecked")
  public static Row parse(String line, String keyField) {
    Object v = Json.parse(line);
    if (!(v instanceof Map)) {
      throw new JoinException("row is not a JSON object: " + abbrev(line));
    }
    Map<String, Object> m = (Map<String, Object>) v;
    if (!m.containsKey(keyField)) {
      throw new JoinException("row is missing key field '" + keyField + "': " + abbrev(line));
    }
    Object k = m.get(keyField);
    if (k == null) {
      return null; // NULL key: never matches, dropped from the join
    }
    if (!(k instanceof Json.Num n) || !n.raw().matches("-?\\d+")) {
      throw new JoinException(
          "key field '" + keyField + "' is not an integer: " + abbrev(line));
    }
    final long key;
    try {
      key = Long.parseLong(n.raw());
    } catch (NumberFormatException e) {
      throw new JoinException("key field '" + keyField + "' is out of 64-bit range: " + n.raw());
    }
    return Row.of(key, line);
  }

  private static String abbrev(String line) {
    return line.length() <= 120 ? line : line.substring(0, 117) + "...";
  }
}
