package com.example.smj;

import java.util.LinkedHashMap;
import java.util.Map;

/** Parameters of one join job. */
public record JoinRequest(
    String leftPath, String rightPath, String keyField, String outputPath,
    long memoryBudgetBytes) {

  public Map<String, Object> toJson() {
    Map<String, Object> m = new LinkedHashMap<>();
    m.put("leftPath", leftPath);
    m.put("rightPath", rightPath);
    m.put("keyField", keyField);
    m.put("outputPath", outputPath);
    m.put("memoryBudgetBytes", new Json.Num(Long.toString(memoryBudgetBytes)));
    return m;
  }
}
