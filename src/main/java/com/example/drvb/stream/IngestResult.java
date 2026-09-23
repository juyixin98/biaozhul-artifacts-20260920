package com.example.drvb.stream;

import com.example.drvb.core.Event;
import com.example.drvb.core.Rule;
import com.example.drvb.core.RuleVersion;

import java.util.ArrayList;
import java.util.List;

/**
 * The outcome of feeding one event into the engine. Either accepted
 * ({@link #accepted()} == true, zero or more matched rules) or rejected with a
 * machine-readable reason.
 */
public record IngestResult(boolean accepted,
                           String rejectionReason,
                           Event event,
                           RuleVersion version,
                           List<RuleMatch> matches,
                           boolean late,
                           long watermark,
                           long processedAt) {

    public record RuleMatch(String ruleId, String ruleName, String action) {
    }

    public static IngestResult accepted(Event event, RuleVersion version,
                                        List<RuleMatch> matches, boolean late,
                                        long watermark, long processedAt) {
        return new IngestResult(true, null, event, version,
                List.copyOf(matches), late, watermark, processedAt);
    }

    public static IngestResult rejected(Event event, String reason,
                                        long watermark, long processedAt) {
        return new IngestResult(false, reason, event, null,
                List.of(), false, watermark, processedAt);
    }

    /** Convenience: ids of matched rules. */
    public List<String> matchedRuleIds() {
        List<String> ids = new ArrayList<>(matches.size());
        for (RuleMatch m : matches) {
            ids.add(m.ruleId());
        }
        return ids;
    }

    static List<RuleMatch> evaluate(RuleVersion version, Event event) {
        List<RuleMatch> out = new ArrayList<>();
        for (Rule rule : version.rules()) {
            if (rule.matches(event)) {
                out.add(new RuleMatch(rule.id(), rule.name(), rule.action()));
            }
        }
        return out;
    }
}
