package com.example.smj;

import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.IOException;

/**
 * Internal spill-file format: one row per line as {@code <key>\t<json>}.
 * Keys are written explicitly so spilled rows can be re-read without re-parsing JSON.
 */
final class SpillFile {

  private SpillFile() {}

  static void writeRow(BufferedWriter w, Row r) throws IOException {
    w.write(Long.toString(r.key()));
    w.write('\t');
    w.write(r.json());
    w.newLine();
  }

  /** @return the next row, or {@code null} at end of file. */
  static Row readRow(BufferedReader r) throws IOException {
    String line = r.readLine();
    if (line == null) {
      return null;
    }
    int tab = line.indexOf('\t');
    long key = Long.parseLong(line.substring(0, tab));
    String json = line.substring(tab + 1);
    return Row.of(key, json);
  }
}
