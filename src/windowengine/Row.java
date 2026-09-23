package windowengine;

import java.util.Arrays;

/**
 * 一行数据。cells 按下标对应 Schema 中的列；sourceIndex 记录该行在输入关系中的
 * 原始位置（0 基），用于保证输出顺序稳定（不随分区排序打乱）。
 */
public final class Row {

    private final Value[] cells;
    private final int sourceIndex;

    public Row(Value[] cells, int sourceIndex) {
        this.cells = cells;
        this.sourceIndex = sourceIndex;
    }

    public Value get(int columnIndex) {
        return cells[columnIndex];
    }

    public Value[] cells() {
        return cells;
    }

    public int sourceIndex() {
        return sourceIndex;
    }

    @Override
    public String toString() {
        return Arrays.toString(cells);
    }
}
