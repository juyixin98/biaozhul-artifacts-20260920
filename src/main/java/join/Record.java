package join;

/**
 * One JSONL record: its join key (null when the JSON key is absent/null/non-integer),
 * and the original raw bytes of the JSON object (written verbatim to output).
 *
 * Raw bytes are retained so output rows never depend on re-serialization and so
 * key extraction only parses what it needs in {@link ExternalSorter} (in fact the
 * whole line is parsed once on read; the bytes are the compact reference form).
 */
public final class Record {

    /** Join key; {@code null} means SQL NULL — never matches anything. */
    public final Long key;

    /** Original JSONL line bytes (UTF-8), without trailing newline. */
    public final byte[] raw;

    public Record(Long key, byte[] raw) {
        this.key = key;
        this.raw = raw;
    }
}
