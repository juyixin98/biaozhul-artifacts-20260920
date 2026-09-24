package bitserver;

/**
 * 固定长度位向量（long[] 实现）。
 *
 * 关键点：位图长度在创建时确定且永不收缩，位的下标始终等于行 ID。
 * 删除行只是把对应位清 0，绝不重新编号、不移位 —— 因此任何压缩编码都必须
 * 能按“原始下标”无损还原（见 {@link RleCodec}）。
 */
public final class Bitmap {

    private final long[] words;
    private final int length;

    public Bitmap(int length) {
        if (length < 0) {
            throw new IllegalArgumentException("length 必须 >= 0");
        }
        this.length = length;
        this.words = new long[wordCount(length)];
    }

    /** 全 1 位图（AND 的单位元）。 */
    public static Bitmap full(int length) {
        Bitmap bm = new Bitmap(length);
        java.util.Arrays.fill(bm.words, -1L);
        bm.maskTail();
        return bm;
    }

    private Bitmap(long[] words, int length) {
        this.words = words;
        this.length = length;
        maskTail();
    }

    static int wordCount(int length) {
        return (length + 63) >>> 6;
    }

    /** 清掉最后一个 word 中超出 length 的尾部位，保证 cardinality 等运算不受脏位影响。 */
    private void maskTail() {
        int rem = length & 63;
        if (rem != 0 && words.length > 0) {
            words[words.length - 1] &= (1L << rem) - 1L;
        }
    }

    public int length() {
        return length;
    }

    public void set(int index) {
        if (index < 0 || index >= length) {
            throw new IndexOutOfBoundsException(index + " 不在 [0," + length + ") 内");
        }
        words[index >>> 6] |= 1L << (index & 63);
    }

    public boolean get(int index) {
        if (index < 0 || index >= length) {
            throw new IndexOutOfBoundsException(index + " 不在 [0," + length + ") 内");
        }
        return (words[index >>> 6] & (1L << (index & 63))) != 0L;
    }

    /** 把 [from, to) 范围的位全部置 1，供 RLE 解码等批量操作使用。 */
    public void setRange(int from, int to) {
        if (from < 0 || to > length || from > to) {
            throw new IndexOutOfBoundsException("非法区间 [" + from + "," + to + "), length=" + length);
        }
        int i = from;
        while (i < to && (i & 63) != 0) {
            words[i >>> 6] |= 1L << (i & 63);
            i++;
        }
        while (i + 64 <= to) {
            words[i >>> 6] = -1L;
            i += 64;
        }
        while (i < to) {
            words[i >>> 6] |= 1L << (i & 63);
            i++;
        }
    }

    /** 就地清掉 o 中为 1 的位（软删除用），长度不变。 */
    public void clear(Bitmap o) {
        sameSize(o);
        for (int i = 0; i < words.length; i++) {
            words[i] &= ~o.words[i];
        }
    }

    /** 就地并入 o（恢复行用），长度不变。 */
    public void merge(Bitmap o) {
        sameSize(o);
        for (int i = 0; i < words.length; i++) {
            words[i] |= o.words[i];
        }
    }

    public Bitmap and(Bitmap o) {
        sameSize(o);
        long[] r = new long[words.length];
        for (int i = 0; i < r.length; i++) {
            r[i] = words[i] & o.words[i];
        }
        return new Bitmap(r, length);
    }

    public Bitmap or(Bitmap o) {
        sameSize(o);
        long[] r = new long[words.length];
        for (int i = 0; i < r.length; i++) {
            r[i] = words[i] | o.words[i];
        }
        return new Bitmap(r, length);
    }

    /** this & ~o（按位差）。 */
    public Bitmap andNot(Bitmap o) {
        sameSize(o);
        long[] r = new long[words.length];
        for (int i = 0; i < r.length; i++) {
            r[i] = words[i] & ~o.words[i];
        }
        return new Bitmap(r, length);
    }

    /** 在整个 [0,length) 全集内取补；尾部多余位始终被掩码为 0。 */
    public Bitmap not() {
        long[] r = new long[words.length];
        for (int i = 0; i < r.length; i++) {
            r[i] = ~words[i];
        }
        return new Bitmap(r, length);
    }

    public Bitmap copy() {
        return new Bitmap(words.clone(), length);
    }

    public int cardinality() {
        int c = 0;
        for (long w : words) {
            c += Long.bitCount(w);
        }
        return c;
    }

    /** 返回置 1 位的下标（即行 ID），升序，下标即原始行号。 */
    public int[] toArray() {
        int[] out = new int[cardinality()];
        int k = 0;
        for (int wi = 0; wi < words.length; wi++) {
            long w = words[wi];
            int base = wi << 6;
            while (w != 0L) {
                out[k++] = base + Long.numberOfTrailingZeros(w);
                w &= w - 1L;
            }
        }
        return out;
    }

    /** 未压缩占用字节数（每个 word 8 字节）。 */
    public int wordBytes() {
        return words.length * Long.BYTES;
    }

    long[] wordsArray() {
        return words;
    }

    private void sameSize(Bitmap o) {
        if (o.length != length) {
            throw new IllegalArgumentException("位图长度不一致: " + length + " vs " + o.length);
        }
    }
}
