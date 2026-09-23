package partjoin;

import java.util.ArrayList;
import java.util.List;

/**
 * Reference nested-loops join used solely as an oracle in tests.
 *
 * It is deliberately simple and independent of the hashing machinery:
 * every left/right pair is compared component by component using
 * {@link Value#equalsValue}, which encodes the NULL-never-matches rule and
 * numeric cross-type equality.
 */
public final class NestedLoopJoin {

    private NestedLoopJoin() {
    }

    public static List<Row> join(Table left, Table right, JoinType type,
                                 List<String> leftKeyColumns, List<String> rightKeyColumns) {
        List<Integer> li = left.schema().indicesOf(leftKeyColumns);
        List<Integer> ri = right.schema().indicesOf(rightKeyColumns);
        List<Row> out = new ArrayList<>();

        for (Row l : left.rows()) {
            boolean matched = false;
            for (Row r : right.rows()) {
                if (keysEqual(l, r, li, ri)) {
                    out.add(concat(l, r));
                    matched = true;
                }
            }
            if (type == JoinType.LEFT && !matched) {
                out.add(concatNulls(l, right.schema().size()));
            }
        }
        return out;
    }

    private static boolean keysEqual(Row l, Row r, List<Integer> li, List<Integer> ri) {
        for (int i = 0; i < li.size(); i++) {
            Value a = l.get(li.get(i));
            Value b = r.get(ri.get(i));
            if (!a.equalsValue(b)) return false; // covers NULL: null.equalsValue -> false
        }
        return true;
    }

    private static Row concat(Row l, Row r) {
        Object[] out = new Object[l.size() + r.size()];
        for (int i = 0; i < l.size(); i++) out[i] = l.get(i).raw();
        for (int i = 0; i < r.size(); i++) out[l.size() + i] = r.get(i).raw();
        return Row.of(out);
    }

    private static Row concatNulls(Row l, int rightWidth) {
        Object[] out = new Object[l.size() + rightWidth];
        for (int i = 0; i < l.size(); i++) out[i] = l.get(i).raw();
        return Row.of(out);
    }
}
