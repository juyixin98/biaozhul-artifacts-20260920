package neardup.core;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * Fixed tokenization + k-shingle rules.
 *
 * Rules (deliberately simple and fully deterministic, no external tokenizer):
 *  1. Lowercase the text (English rules of {@link String#toLowerCase()}).
 *  2. A token is either
 *       - a maximal run of Unicode letters or digits (English words, numbers), or
 *       - a single CJK ideograph (U+4E00..U+9FFF), so Chinese is tokenized per character.
 *  3. The text is split into SEGMENTS on newlines and sentence terminators
 *     (. ! ? 。 ！ ？ ; ；). Shingle windows never cross a segment boundary, so each
 *     sentence/line is shingled independently.
 *  4. Within every segment with at least k tokens, emit all contiguous k-token
 *     windows (k=3 by default). A segment shorter than k tokens contributes NO
 *     shingles. Consequently an extremely short document may have an empty
 *     shingle set; such documents cannot match anything (see README §"短文档反例").
 *  5. Shingles are hashed with 64-bit FNV-1a over the joined token sequence;
 *     the shingle multiset is deduplicated (a Set), i.e. repeated shingles count
 *     once, matching the definition of Jaccard over sets.
 */
public final class Shingler {

    public static final int DEFAULT_K = 3;

    private final int k;

    public Shingler() {
        this(DEFAULT_K);
    }

    public Shingler(int k) {
        if (k < 1) {
            throw new IllegalArgumentException("k must be >= 1, got " + k);
        }
        this.k = k;
    }

    public int k() {
        return k;
    }

    /** Returns the deduplicated 64-bit shingle ids of the document. */
    public Set<Long> shingle(String text) {
        Set<Long> out = new LinkedHashSet<>();
        if (text == null) {
            return out;
        }
        for (List<String> segment : splitSegments(text)) {
            int n = segment.size();
            if (n < k) {
                continue;
            }
            for (int i = 0; i + k <= n; i++) {
                out.add(fnv1a64(joinWindow(segment, i, i + k)));
            }
        }
        return out;
    }

    /** Exposed for tests/debugging: the normalized token sequence, one list per segment. */
    public List<List<String>> tokens(String text) {
        return splitSegments(text);
    }

    private String joinWindow(List<String> seg, int from, int to) {
        StringBuilder sb = new StringBuilder();
        for (int i = from; i < to; i++) {
            if (i > from) {
                sb.append(' ');
            }
            sb.append(seg.get(i));
        }
        return sb.toString();
    }

    private List<List<String>> splitSegments(String text) {
        List<List<String>> segments = new ArrayList<>();
        List<String> current = new ArrayList<>();
        String lower = text.toLowerCase();

        StringBuilder word = new StringBuilder();
        int n = lower.length();
        int i = 0;
        while (i <= n) {
            int cp = i < n ? lower.codePointAt(i) : -1;
            if (cp >= 0) {
                i += Character.charCount(cp);
            } else {
                i++;
            }

            if (cp >= 0 && !isSegmentBreak(cp)
                    && !isCjk(cp) && Character.isLetterOrDigit(cp)) {
                // Accumulate consecutive Latin/digit letters into one word token.
                word.appendCodePoint(cp);
                continue;
            }

            // Any non-word codepoint flushes the accumulated word token.
            if (word.length() > 0) {
                current.add(word.toString());
                word.setLength(0);
            }
            if (cp < 0) {
                break;
            }
            if (isSegmentBreak(cp)) {
                if (!current.isEmpty()) {
                    segments.add(current);
                    current = new ArrayList<>();
                }
            } else if (isCjk(cp)) {
                // Each CJK ideograph is its own token.
                current.add(new String(Character.toChars(cp)));
            }
            // Any other punctuation / whitespace: just a token gap.
        }
        if (!current.isEmpty()) {
            segments.add(current);
        }
        return segments;
    }

    private static boolean isSegmentBreak(int cp) {
        return cp == '\n' || cp == '\r'
                || cp == '.' || cp == '!' || cp == '?'
                || cp == 0x3002 // 。
                || cp == 0xFF01 // ！
                || cp == 0xFF1F // ？
                || cp == ';' || cp == 0xFF1B; // ; ；
    }

    private static boolean isCjk(int cp) {
        return cp >= 0x4E00 && cp <= 0x9FFF;
    }

    /** FNV-1a 64-bit, computed over UTF-16 char bytes of the joined window. */
    static long fnv1a64(String s) {
        long hash = 0xcbf29ce484222325L;
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            hash ^= (c & 0xff);
            hash *= 0x100000001b3L;
            hash ^= ((c >>> 8) & 0xff);
            hash *= 0x100000001b3L;
        }
        return hash;
    }
}
