package approx.stream;

import java.util.Objects;

/**
 * One observed event: an item at an event time.
 *
 * @param <T> item type; the JSON service uses {@code String}.
 */
public final class Event<T> {
    public final T item;
    public final long timestampMillis;

    public Event(T item, long timestampMillis) {
        this.item = Objects.requireNonNull(item, "item");
        this.timestampMillis = timestampMillis;
    }
}
