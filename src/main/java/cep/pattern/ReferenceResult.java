package cep.pattern;

import cep.model.Match;
import cep.model.Timeout;

import java.util.List;

/** {@link BruteForceMatcher} 的结果（不含统计对象依赖，直接用计数字段）。 */
public record ReferenceResult(List<Match> matches, List<Timeout> timeouts,
                              long matchCount, long timeoutCount,
                              long killedCount, long processedCount) {

    public ReferenceResult {
        matches = List.copyOf(matches);
        timeouts = List.copyOf(timeouts);
    }
}
