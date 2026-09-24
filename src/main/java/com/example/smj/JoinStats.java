package com.example.smj;

import java.util.LinkedHashMap;
import java.util.Map;

/** Counters describing one finished join run (sort spill, group spill, output size). */
public final class JoinStats {
  public long leftRows;
  public long rightRows;
  public long leftNullKeysDropped;
  public long rightNullKeysDropped;
  public long leftRuns;
  public long rightRuns;
  /** Total bytes written to sort spill files, including merge passes. */
  public long sortSpillWriteBytes;
  public long matchedKeyGroups;
  /** Number of key groups (left/right counted separately) that spilled to disk. */
  public long spilledKeyGroups;
  public long groupSpillWriteBytes;
  public long outputRows;

  public Map<String, Object> toJson() {
    Map<String, Object> m = new LinkedHashMap<>();
    m.put("leftRows", num(leftRows));
    m.put("rightRows", num(rightRows));
    m.put("leftNullKeysDropped", num(leftNullKeysDropped));
    m.put("rightNullKeysDropped", num(rightNullKeysDropped));
    m.put("leftRuns", num(leftRuns));
    m.put("rightRuns", num(rightRuns));
    m.put("sortSpillWriteBytes", num(sortSpillWriteBytes));
    m.put("matchedKeyGroups", num(matchedKeyGroups));
    m.put("spilledKeyGroups", num(spilledKeyGroups));
    m.put("groupSpillWriteBytes", num(groupSpillWriteBytes));
    m.put("outputRows", num(outputRows));
    return m;
  }

  private static Json.Num num(long v) {
    return new Json.Num(Long.toString(v));
  }
}
