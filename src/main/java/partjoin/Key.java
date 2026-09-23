package partjoin;

import java.util.Arrays;
import java.util.List;

/**
 * Composite join key (one value per key column).
 *
 * SQL semantics: a key is null-keyed if ANY component is NULL; null keys
 * never compare equal (even when all components are identical), which the
 * engine handles by routing null-keyed rows away from hash partitions.
 */
public final class Key {

    private final Value[] components;
    private final int hash;

    private Key(Value[] components) {
        this.components = components;
        int h = 1;
        for (Value v : components) h = 31 * h + v.hashValue();
        this.hash = h;
    }

    public static Key of(Row r, List<Integer> keyIndices) {
        Value[] cs = new Value[keyIndices.size()];
        for (int i = 0; i < cs.length; i++) cs[i] = r.get(keyIndices.get(i));
        return new Key(cs);
    }

    public boolean hasNull() {
        for (Value v : components) if (v.isNull()) return true;
        return false;
    }

    public int componentCount() {
        return components.length;
    }

    @Override
    public boolean equals(Object o) {
        if (!(o instanceof Key other) || other.components.length != components.length) return false;
        for (int i = 0; i < components.length; i++) {
            if (!components[i].equalsValue(other.components[i])) return false;
        }
        return true;
    }

    @Override
    public int hashCode() {
        return hash;
    }

    @Override
    public String toString() {
        return Arrays.toString(components);
    }
}
