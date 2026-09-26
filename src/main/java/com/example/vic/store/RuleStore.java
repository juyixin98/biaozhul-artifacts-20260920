package com.example.vic.store;

import com.example.vic.domain.RuleDef;
import com.example.vic.domain.VersionDef;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;

/**
 * Immutable holder of versions and rules. Every mutating method validates the change
 * and returns a NEW store; the receiver is never modified.
 */
public record RuleStore(Map<String, VersionDef> versions, Map<String, RuleDef> rules) {

    public RuleStore {
        versions = Map.copyOf(versions);
        rules = Map.copyOf(rules);
    }

    public static RuleStore empty() {
        return new RuleStore(Map.of(), Map.of());
    }

    public RuleStore withVersion(VersionDef version) {
        Map<String, VersionDef> next = new LinkedHashMap<>(versions);
        next.put(version.id(), version);
        return new RuleStore(next, rules);
    }

    public RuleStore withoutVersion(String versionId) {
        if (!versions.containsKey(versionId)) {
            throw new NotFoundException("version not found: " + versionId);
        }
        Map<String, VersionDef> nextVersions = new LinkedHashMap<>(versions);
        nextVersions.remove(versionId);
        Map<String, RuleDef> nextRules = new LinkedHashMap<>();
        rules.forEach((id, rule) -> {
            if (!rule.versionId().equals(versionId)) {
                nextRules.put(id, rule);
            }
        });
        return new RuleStore(nextVersions, nextRules);
    }

    /**
     * Adds a rule after validating it. Rejects (throws {@link ConflictException}) when the
     * new rule overlaps any existing rule whose version has the SAME priority — equal-priority
     * overlap is ambiguous and never silently resolved.
     */
    public RuleStore withRule(RuleDef rule) {
        VersionDef owner = versions.get(rule.versionId());
        if (owner == null) {
            throw new NotFoundException("version not found: " + rule.versionId());
        }
        if (rules.containsKey(rule.id())) {
            throw new ConflictException("rule id already exists: " + rule.id());
        }
        for (RuleDef existing : rules.values()) {
            if (!existing.interval().overlaps(rule.interval())) {
                continue;
            }
            int existingPriority = versions.get(existing.versionId()).priority();
            if (existingPriority == owner.priority()) {
                throw new ConflictException(
                        "equal-priority conflict: rule " + rule.id()
                                + " (version " + rule.versionId() + ", priority " + owner.priority() + ")"
                                + " overlaps rule " + existing.id()
                                + " (version " + existing.versionId() + ", priority " + existingPriority + ")"
                                + " on " + rule.interval());
            }
        }
        Map<String, RuleDef> next = new LinkedHashMap<>(rules);
        next.put(rule.id(), rule);
        return new RuleStore(versions, next);
    }

    public RuleStore withoutRule(String ruleId) {
        if (!rules.containsKey(ruleId)) {
            throw new NotFoundException("rule not found: " + ruleId);
        }
        Map<String, RuleDef> next = new LinkedHashMap<>(rules);
        next.remove(ruleId);
        return new RuleStore(versions, next);
    }

    public Optional<VersionDef> version(String versionId) {
        return Optional.ofNullable(versions.get(versionId));
    }
}
