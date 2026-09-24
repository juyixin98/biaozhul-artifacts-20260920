package com.example.smj;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Random;

/** Shared helpers for the test suite. */
final class TestUtil {

  private TestUtil() {}

  static void writeJsonl(Path p, List<String> lines) throws IOException {
    Files.write(p, lines, StandardCharsets.UTF_8);
  }

  /** All lines of a file, sorted — used for multiset comparison. */
  static List<String> readSorted(Path p) throws IOException {
    List<String> lines = Files.readAllLines(p, StandardCharsets.UTF_8);
    lines.sort(String::compareTo);
    return lines;
  }

  /**
   * Reference equi-join computed naively in memory. Returns the expected output lines
   * (same {@code {"left":...,"right":...}} format as the engine), sorted for multiset
   * comparison. NULL keys never match.
   */
  static List<String> naiveJoin(Path left, Path right, String keyField) throws IOException {
    Map<Long, List<String>> rightByKey = new HashMap<>();
    for (String line : Files.readAllLines(right, StandardCharsets.UTF_8)) {
      String t = line.trim();
      if (t.isEmpty()) continue;
      Row r = RowParser.parse(t, keyField);
      if (r == null) continue;
      rightByKey.computeIfAbsent(r.key(), k -> new ArrayList<>()).add(t);
    }
    List<String> out = new ArrayList<>();
    for (String line : Files.readAllLines(left, StandardCharsets.UTF_8)) {
      String t = line.trim();
      if (t.isEmpty()) continue;
      Row l = RowParser.parse(t, keyField);
      if (l == null) continue;
      List<String> matches = rightByKey.get(l.key());
      if (matches == null) continue;
      for (String rj : matches) {
        out.add("{\"left\":" + t + ",\"right\":" + rj + "}");
      }
    }
    out.sort(String::compareTo);
    return out;
  }

  /**
   * Generate {@code n} rows with keys drawn from {@code [0, keySpace)} plus a padding
   * field so each row is roughly {@code approxBytes} of JSON.
   */
  static List<String> genRows(int n, int keySpace, int approxBytes, long seed) {
    Random rnd = new Random(seed);
    String pad = "p".repeat(Math.max(0, approxBytes - 60));
    List<String> rows = new ArrayList<>(n);
    for (int i = 0; i < n; i++) {
      rows.add("{\"k\":" + rnd.nextInt(keySpace) + ",\"v\":\"r" + i + "\",\"pad\":\"" + pad + "\"}");
    }
    return rows;
  }

  /** Generate {@code n} rows that all share one hot key. */
  static List<String> genHotRows(int n, long hotKey, int approxBytes, String tag) {
    String pad = "h".repeat(Math.max(0, approxBytes - 60));
    List<String> rows = new ArrayList<>(n);
    for (int i = 0; i < n; i++) {
      rows.add("{\"k\":" + hotKey + ",\"v\":\"" + tag + i + "\",\"pad\":\"" + pad + "\"}");
    }
    return rows;
  }
}
