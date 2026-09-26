package com.example.eventorder.engine;

import com.example.eventorder.model.Conflict;
import com.example.eventorder.model.DependencySpec;
import java.util.ArrayList;
import java.util.List;

/**
 * Version-order rule: when enabled, a dependency u -&gt; v whose endpoints both
 * carry versions requires {@code version(u) <= version(v)}. Events without
 * versions are exempt (rule applies only where both sides are versioned).
 */
public final class VersionRuleChecker {

    private VersionRuleChecker() {
    }

    public static List<Conflict> check(ValidatedInput input) {
        if (!input.enforceVersionOrder()) {
            return List.of();
        }
        List<Conflict> conflicts = new ArrayList<>();
        for (DependencySpec dep : input.dependencies()) {
            Long beforeVersion = input.events().get(dep.before()).version();
            Long afterVersion = input.events().get(dep.after()).version();
            if (beforeVersion != null && afterVersion != null
                    && beforeVersion > afterVersion) {
                conflicts.add(new Conflict("VERSION_CONTRADICTION",
                        List.of(dep.before(), dep.after()),
                        "version-order rule requires version('" + dep.before() + "') <= version('"
                                + dep.after() + "') along the dependency, but "
                                + beforeVersion + " > " + afterVersion));
            }
        }
        return conflicts;
    }
}
