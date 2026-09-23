package joinorder;

/** 等值连接谓词：left = right（一条边的一个等值条件，一条边可有多个条件）。 */
public final class EqPredicate {
    public final ColumnRef left;
    public final ColumnRef right;

    public EqPredicate(ColumnRef left, ColumnRef right) {
        this.left = left;
        this.right = right;
    }

    /** 返回落在 sideMask 这一侧的列；若谓词不涉及该侧返回 null。 */
    public ColumnRef columnOnSide(int sideMask, Model model) {
        int li = 1 << model.indexOf(left.table);
        if ((sideMask & li) != 0) return left;
        int ri = 1 << model.indexOf(right.table);
        if ((sideMask & ri) != 0) return right;
        return null;
    }

    @Override public String toString() { return left + " = " + right; }
}
