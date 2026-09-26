package com.dstexp.model;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;

/**
 * A single rule specification.
 *
 * <ul>
 *   <li>{@code type = "daily"}: recurring every day at {@code localTime} (HH:mm or HH:mm:ss).</li>
 *   <li>{@code type = "cron"}:  five-field cron expression ({@code cron}) with minute granularity,
 *       supporting {@code *} / {@code ,} / {@code -} / {@code /} and numeric month/DOW fields.</li>
 * </ul>
 */
@JsonIgnoreProperties(ignoreUnknown = true)
public class RuleSpec {

    private String ruleId;
    private String type;
    private String localTime;
    private String cron;

    public String getRuleId() { return ruleId; }
    public void setRuleId(String ruleId) { this.ruleId = ruleId; }

    public String getType() { return type; }
    public void setType(String type) { this.type = type; }

    public String getLocalTime() { return localTime; }
    public void setLocalTime(String localTime) { this.localTime = localTime; }

    public String getCron() { return cron; }
    public void setCron(String cron) { this.cron = cron; }
}
