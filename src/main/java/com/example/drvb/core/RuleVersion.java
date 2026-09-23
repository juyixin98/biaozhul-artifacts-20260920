package com.example.drvb.core;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;

/**
 * An immutable, named snapshot of the full rule set, effective for events whose
 * event time falls into
 * {@code [effectiveFrom, nextVersion.effectiveFrom)}.
 *
 * <p>Invariants enforced at construction:
 * <ul>
 *   <li>versions are identified by a unique, non-blank {@code versionId};</li>
 *   <li>rule ids are unique within a version;</li>
 *   <li>the rule list order is preserved and significant (matches are returned
 *       in that order);</li>
 *   <li>{@code checksum} is the SHA-256 of the canonical JSON of the rule set,
 *       so identical content (e.g. produced by a rollback) yields an identical
 *       checksum even under a different version id.</li>
 * </ul>
 *
 * <p>Instances are immutable and safe to share across threads.
 */
public final class RuleVersion {

    private final String versionId;
    private final long effectiveFrom;
    private final long createdAt;
    private final String description;
    private final List<Rule> rules;
    private final Map<String, Rule> rulesById;
    private final String checksum;

    public RuleVersion(String versionId, long effectiveFrom, long createdAt,
                       String description, List<Rule> rules) {
        this.versionId = requireNonBlank(versionId, "versionId");
        this.effectiveFrom = effectiveFrom;
        this.createdAt = createdAt;
        this.description = description == null ? "" : description;
        List<Rule> copy = List.copyOf(Objects.requireNonNull(rules, "rules"));
        Set<String> ids = new HashSet<>();
        for (Rule r : copy) {
            if (!ids.add(r.id())) {
                throw new IllegalArgumentException("duplicate rule id in version "
                        + versionId + ": " + r.id());
            }
        }
        this.rules = copy;
        Map<String, Rule> byId = new LinkedHashMap<>();
        for (Rule r : copy) {
            byId.put(r.id(), r);
        }
        this.rulesById = Collections.unmodifiableMap(byId);
        this.checksum = sha256(JsonCanonical.write(rulesToMap(copy)));
    }

    public String versionId() {
        return versionId;
    }

    public long effectiveFrom() {
        return effectiveFrom;
    }

    public long createdAt() {
        return createdAt;
    }

    public String description() {
        return description;
    }

    public List<Rule> rules() {
        return rules;
    }

    public Rule rule(String id) {
        return rulesById.get(id);
    }

    /** SHA-256 of the canonical rule-set JSON, hex-encoded. */
    public String checksum() {
        return checksum;
    }

    /** JSON-ready map of this version (as returned by the HTTP API). */
    public Map<String, Object> toMap() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("versionId", versionId);
        m.put("effectiveFrom", effectiveFrom);
        m.put("createdAt", createdAt);
        m.put("description", description);
        m.put("checksum", checksum);
        m.put("rules", rulesToMap(rules));
        return m;
    }

    static List<Object> rulesToMap(List<Rule> rs) {
        List<Object> out = new ArrayList<>(rs.size());
        for (Rule r : rs) {
            out.add(r.toMap());
        }
        return out;
    }

    private static String requireNonBlank(String s, String what) {
        if (s == null || s.isBlank()) {
            throw new IllegalArgumentException(what + " is required");
        }
        return s;
    }

    private static String sha256(String input) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(input.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(digest.length * 2);
            for (byte b : digest) {
                hex.append(String.format("%02x", b));
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 unavailable", e);
        }
    }
}
