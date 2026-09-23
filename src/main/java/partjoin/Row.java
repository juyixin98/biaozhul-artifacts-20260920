package partjoin;

import java.util.ArrayList;
import java.util.List;

/** An ordered tuple of {@link Value}s. */
public final class Row {

    private final Value[] values;

    private Row(Value[] values) {
        this.values = values;
    }

    public static Row of(Object... raw) {
        Value[] vs = new Value[raw.length];
        for (int i = 0; i < raw.length; i++) vs[i] = Value.of(raw[i]);
        return new Row(vs);
    }

    public static Row ofValues(List<Value> values) {
        return new Row(values.toArray(new Value[0]));
    }

    @SuppressWarnings("rawtypes")
    public static Row ofRaw(List raw) {
        Value[] vs = new Value[raw.size()];
        for (int i = 0; i < raw.size(); i++) vs[i] = Value.of(raw.get(i));
        return new Row(vs);
    }

    public int size() {
        return values.length;
    }

    public Value get(int i) {
        return values[i];
    }

    public List<Value> values() {
        return List.of(values);
    }

    public List<Object> rawList() {
        List<Object> out = new ArrayList<>(values.length);
        for (Value v : values) out.add(v.raw());
        return out;
    }

    @Override
    public boolean equals(Object o) {
        if (!(o instanceof Row other) || other.values.length != values.length) return false;
        for (int i = 0; i < values.length; i++) {
            if (!values[i].equalsValue(other.values[i])) return false;
        }
        return true;
    }

    @Override
    public int hashCode() {
        int h = 1;
        for (Value v : values) h = 31 * h + v.hashValue();
        return h;
    }

    @Override
    public String toString() {
        return rawList().toString();
    }
}
