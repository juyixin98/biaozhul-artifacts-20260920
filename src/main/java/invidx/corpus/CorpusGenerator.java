package invidx.corpus;

import java.util.ArrayList;
import java.util.List;
import java.util.Random;

/**
 * Deterministic synthetic corpus. Documents are generated from a fixed
 * vocabulary across several topics so queries have non-trivial posting
 * lists, with shared stop-word-like terms that appear in almost every
 * document.
 */
public final class CorpusGenerator {

    public record Spec(int docCount, long seed) {
    }

    private static final String[][] TOPIC_WORDS = {
            {"lucene", "index", "segment", "merge", "posting", "token", "inverted", "analyzer"},
            {"coffee", "espresso", "beans", "roast", "grinder", "crema", "barista", "latte"},
            {"orbit", "rocket", "satellite", "launch", "thrust", "payload", "trajectory", "lunar"},
            {"garden", "tomato", "basil", "compost", "harvest", "seedling", "watering", "pergola"},
            {"market", "equity", "bond", "yield", "portfolio", "dividend", "trader", "quarterly"},
    };

    private static final String[] COMMON = {"the", "quick", "report", "notes", "daily", "update"};
    private static final String[] TEMPLATES = {
            "%s %s about %s and %s today",
            "weekly %s : %s, %s, %s matters now",
            "how %s met %s — a %s story featuring %s",
            "%s vs %s in the %s desk, plus %s",
            "notes on %s, %s and why %s follows %s",
    };

    private final Random random;

    public CorpusGenerator(long seed) {
        this.random = new Random(seed);
    }

    public List<String> generate(int count) {
        List<String> out = new ArrayList<>(count);
        for (int i = 0; i < count; i++) {
            out.add(generateOne(i));
        }
        return out;
    }

    private String generateOne(int i) {
        int topic = i % TOPIC_WORDS.length;
        String[] words = TOPIC_WORDS[topic];
        String a = words[random.nextInt(words.length)];
        String b = words[random.nextInt(words.length)];
        String c = words[random.nextInt(words.length)];
        String d = words[random.nextInt(words.length)];
        String common = COMMON[random.nextInt(COMMON.length)];
        String template = TEMPLATES[random.nextInt(TEMPLATES.length)];
        String text = String.format(template, a, b, c, d);
        if (random.nextInt(4) == 0) {
            text = common + " " + text;
        }
        return text;
    }

    /** A term guaranteed to occur in roughly 1/5 of the seeded docs. */
    public static String guaranteedTopicTerm(int topic) {
        return TOPIC_WORDS[topic][0];
    }
}
