package com.example.recur.model;

/**
 * 月度展开时，目标月份不存在起始日（如 31 日遇 2 月）的显式策略。
 *
 * <ul>
 *   <li>{@link #LAST_VALID_DAY}：回退到该月最后一个有效日（31 日 -&gt; 28/29/30 日）。</li>
 *   <li>{@link #SKIP}：跳过不存在该日期的月份，不产生实例。</li>
 * </ul>
 */
public enum MonthEndPolicy {
  LAST_VALID_DAY,
  SKIP
}
