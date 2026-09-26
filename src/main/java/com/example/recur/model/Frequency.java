package com.example.recur.model;

/** 支持的频率子集：日、周、月。不支持年频率及 BYSETPOS/BYDAY 等完整 RFC 5545 特性。 */
public enum Frequency {
  DAILY,
  WEEKLY,
  MONTHLY
}
