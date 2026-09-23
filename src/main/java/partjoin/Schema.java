package partjoin;

import java.util.ArrayList;
import java.util.List;

/** Ordered column names. Duplicate names are allowed. */
public final class Schema {

    private final List<String> columns;

    public Schema(List<String> columns) {
        this.columns = List.copyOf(columns);
    }

    public static Schema of(String... columns) {
        return new Schema(List.of(columns));
    }

    public int size() {
        return columns.size();
    }

    public String column(int i) {
        return columns.get(i);
    }

    public List<String> columns() {
        return columns;
    }

    /** Resolves a column name, throwing when the name is missing or ambiguous. */
    public int indexOf(String name) {
        int found = -1;
        for (int i = 0; i < columns.size(); i++) {
            if (columns.get(i).equals(name)) {
                if (found != -1) {
                    throw new JoinException(JoinException.INVALID_REQUEST,
                            "Ambiguous column name: " + name);
                }
                found = i;
            }
        }
        if (found == -1) {
            throw new JoinException(JoinException.INVALID_REQUEST,
                    "Unknown column: " + name + " (columns: " + columns + ")");
        }
        return found;
    }

    public List<Integer> indicesOf(List<String> names) {
        List<Integer> out = new ArrayList<>();
        for (String n : names) out.add(indexOf(n));
        return out;
    }

    @Override
    public String toString() {
        return columns.toString();
    }
}
