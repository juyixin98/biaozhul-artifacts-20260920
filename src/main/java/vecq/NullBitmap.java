package vecq;

/**
 * NULL 位图：与列的值数组分离存储。
 *
 * 位图中 bit=1 表示该行是 NULL。一个全非空列允许 bitmap 为 null（省内存的常见做法），
 * {@link Column#isNull(int)} 会把“没有位图”视为全部非空。
 */
public final class NullBitmap {

    private final long[] words;
    private final int size;
    private int nullCount;

    private NullBitmap(int size) {
        this.size = size;
        this.words = new long[(size + 63) >>> 6];
    }

    /** 构造一个全部非空的位图。 */
    public static NullBitmap allPresent(int size) {
        return new NullBitmap(size);
    }

    /** 从 NULL 行下标集合构造。 */
    public static NullBitmap fromNullRows(int size, int[] nullRows) {
        NullBitmap bm = new NullBitmap(size);
        for (int row : nullRows) {
            if (row < 0 || row >= size) {
                throw new InvalidQueryException("NULL 行下标越界: " + row + "（列长度 " + size + "）");
            }
            if (!bm.get(row)) bm.nullCount++;
            bm.set(row);
        }
        return bm;
    }

    private boolean get(int row) {
        return (words[row >>> 6] & (1L << row)) != 0L;
    }

    private void set(int row) {
        words[row >>> 6] |= 1L << row;
    }

    public boolean isNull(int row) {
        if (row < 0 || row >= size) {
            throw new IndexOutOfBoundsException("行下标越界: " + row + "（列长度 " + size + "）");
        }
        return get(row);
    }

    public boolean isPresent(int row) {
        return !isNull(row);
    }

    public int size() {
        return size;
    }

    public int nullCount() {
        return nullCount;
    }

    /** 返回 NULL 行的下标的有序数组（用于序列化 / 测试）。 */
    public int[] nullRows() {
        int[] out = new int[nullCount];
        int k = 0;
        for (int row = 0; row < size; row++) {
            if (get(row)) out[k++] = row;
        }
        return out;
    }
}
