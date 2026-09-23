package bitmapindex;

import java.util.Arrays;

/**
 * A compressed immutable/mutable bitset, Roaring-style.
 *
 * Space: the 32-bit row-id space is split into chunks (high 16 bits = chunk key,
 * low 16 bits = position inside the chunk). Each chunk is one of:
 *   - ArrayContainer: sorted short[] of set positions (sparse, cardinality <= 4096)
 *   - BitmapContainer: long[1024] (dense, cardinality > 4096)
 *   - RunContainer: sorted disjoint runs (used for complement results with long runs)
 *
 * The number of set bits and the highest set bit are tracked exactly. Row ids are
 * plain integers; compression never renumbers or reorders them: iterating the
 * bitmap always yields the original integer row ids in ascending order.
 */
public final class RoaringBitmap {

    static final int CHUNK_BITS = 16;
    static final int CHUNK_SIZE = 1 << CHUNK_BITS; // 65536
    static final int CHUNK_MASK = CHUNK_SIZE - 1;
    static final int ARRAY_MAX = 4096; // dense/sparse crossover (half of 8192 shorts)

    private short[] keys = new short[0];
    private Container[] containers = new Container[0];
    private int size; // number of populated chunks

    public RoaringBitmap() {
    }

    /* ------------------------------------------------------------------ */
    /* construction                                                        */
    /* ------------------------------------------------------------------ */

    public static RoaringBitmap of(int... values) {
        RoaringBitmap b = new RoaringBitmap();
        for (int v : values) {
            b.add(v);
        }
        return b;
    }

    /** Bulk build from a sorted, unique array of non-negative ids (row ids unchanged). */
    public static RoaringBitmap fromSorted(int[] sorted) {
        RoaringBitmap b = new RoaringBitmap();
        int i = 0;
        while (i < sorted.length) {
            int key = sorted[i] >>> CHUNK_BITS;
            int start = i;
            while (i < sorted.length && (sorted[i] >>> CHUNK_BITS) == key) {
                i++;
            }
            int card = i - start;
            short[] vals = new short[card];
            for (int k = 0; k < card; k++) {
                vals[k] = (short) (sorted[start + k] & CHUNK_MASK);
            }
            Container c;
            if (card <= ARRAY_MAX) {
                c = new ArrayContainer(vals, card);
            } else {
                long[] words = new long[1024];
                for (short v : vals) {
                    int p = v & 0xffff;
                    words[p >>> 6] |= 1L << (p & 63);
                }
                c = new BitmapContainer(words, card);
            }
            b.append(key, c);
        }
        return b;
    }

    /** Inverse of {@link #toArray()} for building a dense range quickly. */
    public static RoaringBitmap range(int fromInclusive, int toExclusive) {
        RoaringBitmap b = new RoaringBitmap();
        if (fromInclusive >= toExclusive) {
            return b;
        }
        if (fromInclusive < 0) {
            throw new IllegalArgumentException("negative row id");
        }
        int firstChunk = fromInclusive >>> CHUNK_BITS;
        int lastChunk = (toExclusive - 1) >>> CHUNK_BITS;
        for (int chunk = firstChunk; chunk <= lastChunk; chunk++) {
            int chunkStart = chunk << CHUNK_BITS;
            int loAbs = Math.max(fromInclusive, chunkStart);
            int hiAbs = Math.min(toExclusive, chunkStart + CHUNK_SIZE);
            int card = hiAbs - loAbs;
            int lo = loAbs & CHUNK_MASK;      // first position in chunk
            int end = hiAbs - chunkStart;    // one past last position, 1..65536
            Container c;
            if (card <= ARRAY_MAX) {
                short[] vals = new short[card];
                for (int k = 0; k < card; k++) {
                    vals[k] = (short) (lo + k);
                }
                c = new ArrayContainer(vals, card);
            } else {
                long[] words = new long[1024];
                int firstWord = lo >>> 6;
                int lastWord = (end - 1) >>> 6;
                for (int w = firstWord; w <= lastWord; w++) {
                    long word = ~0L;
                    if (w == firstWord) {
                        word &= ~0L << (lo & 63);
                    }
                    if (w == lastWord && (end & 63) != 0) {
                        word &= (1L << (end & 63)) - 1;
                    }
                    words[w] = word;
                }
                c = new BitmapContainer(words, card);
            }
            b.append(chunk, c);
        }
        return b;
    }

    /** Remove one id (used to maintain the live-universe mask on deletion). */
    public void remove(int x) {
        if (x < 0) {
            return;
        }
        int idx = chunkIndex((short) (x >>> CHUNK_BITS));
        if (idx < 0) {
            return;
        }
        Container c = containers[idx].remove(x & CHUNK_MASK);
        if (c.cardinality == 0) {
            System.arraycopy(keys, idx + 1, keys, idx, size - idx - 1);
            System.arraycopy(containers, idx + 1, containers, idx, size - idx - 1);
            size--;
            keys[size] = 0;
            containers[size] = null;
        } else {
            containers[idx] = c;
        }
    }

    /** Number of containers by kind: result[0]=array, result[1]=bitmap. */
    public int[] containerTypeCounts() {
        int a = 0, bm = 0;
        for (int i = 0; i < size; i++) {
            if (containers[i] instanceof ArrayContainer) {
                a++;
            } else {
                bm++;
            }
        }
        return new int[] {a, bm};
    }

    public void add(int x) {
        if (x < 0) {
            throw new IllegalArgumentException("negative row id: " + x);
        }
        int hk = x >>> CHUNK_BITS;
        short key = (short) hk;
        int idx = chunkIndex(key);
        if (idx >= 0) {
            containers[idx] = containers[idx].add(x & CHUNK_MASK);
        } else {
            int at = -idx - 1;
            insertChunk(at, key, new ArrayContainer(new short[] {(short) (x & CHUNK_MASK)}, 1));
        }
    }

    /* ------------------------------------------------------------------ */
    /* queries                                                             */
    /* ------------------------------------------------------------------ */

    public boolean contains(int x) {
        if (x < 0) {
            return false;
        }
        int idx = chunkIndex((short) (x >>> CHUNK_BITS));
        return idx >= 0 && containers[idx].contains(x & CHUNK_MASK);
    }

    public int cardinality() {
        int c = 0;
        for (int i = 0; i < size; i++) {
            c += containers[i].cardinality;
        }
        return c;
    }

    public boolean isEmpty() {
        for (int i = 0; i < size; i++) {
            if (containers[i].cardinality != 0) {
                return false;
            }
        }
        return true;
    }

    /** Highest set bit + 1; 0 when empty. Identifies the universe [0, capacity()). */
    public long capacity() {
        if (size == 0) {
            return 0;
        }
        Container last = containers[size - 1];
        return ((long) (keys[size - 1] & 0xffff) << CHUNK_BITS) + (last.last() & 0xffff) + 1;
    }

    public int[] toArray() {
        int card = cardinality();
        int[] out = new int[card];
        int p = 0;
        for (int i = 0; i < size; i++) {
            int base = (keys[i] & 0xffff) << CHUNK_BITS;
            short[] vals = containers[i].values();
            for (short v : vals) {
                out[p++] = base + (v & 0xffff);
            }
        }
        return out;
    }

    /* ------------------------------------------------------------------ */
    /* set algebra                                                         */
    /* ------------------------------------------------------------------ */

    public RoaringBitmap copy() {
        RoaringBitmap b = new RoaringBitmap();
        b.keys = Arrays.copyOf(keys, size);
        b.containers = new Container[size];
        for (int i = 0; i < size; i++) {
            b.containers[i] = containers[i].copy();
        }
        b.size = size;
        return b;
    }

    public static RoaringBitmap and(RoaringBitmap a, RoaringBitmap b) {
        RoaringBitmap out = new RoaringBitmap();
        int i = 0, j = 0;
        while (i < a.size && j < b.size) {
            int ka = a.keys[i] & 0xffff;
            int kb = b.keys[j] & 0xffff;
            if (ka == kb) {
                Container c = a.containers[i].and(b.containers[j]);
                if (c.cardinality > 0) {
                    out.append(ka, c);
                }
                i++;
                j++;
            } else if (ka < kb) {
                i++;
            } else {
                j++;
            }
        }
        return out;
    }

    public static RoaringBitmap or(RoaringBitmap a, RoaringBitmap b) {
        RoaringBitmap out = new RoaringBitmap();
        int i = 0, j = 0;
        while (i < a.size || j < b.size) {
            if (j == b.size || (i < a.size && (a.keys[i] & 0xffff) < (b.keys[j] & 0xffff))) {
                out.append(a.keys[i] & 0xffff, a.containers[i].copy());
                i++;
            } else if (i == a.size || (b.keys[j] & 0xffff) < (a.keys[i] & 0xffff)) {
                out.append(b.keys[j] & 0xffff, b.containers[j].copy());
                j++;
            } else {
                out.append(a.keys[i] & 0xffff, a.containers[i].or(b.containers[j]));
                i++;
                j++;
            }
        }
        return out;
    }

    /**
     * Complement restricted to the currently-live row universe:
     * result = universe \\ this, i.e. {@code universe AND NOT this}.
     * Deleted rows are part of the universe bounds but excluded from the
     * universe bitset, so they can never leak into a NOT result.
     */
    public static RoaringBitmap notWithin(RoaringBitmap bits, RoaringBitmap universe) {
        RoaringBitmap out = new RoaringBitmap();
        int i = 0; // index into bits
        int j = 0; // index into universe
        while (j < universe.size) {
            int ku = universe.keys[j] & 0xffff;
            Container uc = universe.containers[j];
            if (i < bits.size && ((bits.keys[i] & 0xffff) == ku)) {
                // universe chunk minus the bits chunk (note the operand order:
                // andNot computes this \ o, and we want universe \ bits)
                Container c = uc.andNot(bits.containers[i]);
                if (c.cardinality > 0) {
                    out.append(ku, c);
                }
                i++;
            } else {
                // whole universe chunk survives (bits has nothing here)
                Container c = uc.copy();
                if (c.cardinality > 0) {
                    out.append(ku, c);
                }
            }
            j++;
        }
        return out;
    }

    /** Difference: a \\ b. */
    public static RoaringBitmap andNot(RoaringBitmap a, RoaringBitmap b) {
        RoaringBitmap out = new RoaringBitmap();
        int i = 0, j = 0;
        while (i < a.size) {
            int ka = a.keys[i] & 0xffff;
            if (j < b.size && ((b.keys[j] & 0xffff) == ka)) {
                Container c = a.containers[i].andNot(b.containers[j]);
                if (c.cardinality > 0) {
                    out.append(ka, c);
                }
                i++;
                j++;
            } else if (j < b.size && (b.keys[j] & 0xffff) < ka) {
                j++;
            } else {
                out.append(ka, a.containers[i].copy());
                i++;
            }
        }
        return out;
    }

    /* ------------------------------------------------------------------ */
    /* space statistics                                                    */
    /* ------------------------------------------------------------------ */

    /** Bytes used by this bitmap's data containers (excludes the small key array). */
    public long serializedSizeBytes() {
        long bytes = 0;
        for (int i = 0; i < size; i++) {
            bytes += containers[i].byteSize();
        }
        return bytes;
    }

    public int chunkCount() {
        return size;
    }

    public String debugContainers() {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < size; i++) {
            if (i > 0) {
                sb.append(", ");
            }
            sb.append(keys[i] & 0xffff).append(':')
              .append(containers[i].getClass().getSimpleName().replace("Container", ""))
              .append('(').append(containers[i].cardinality).append(')');
        }
        return sb.toString();
    }

    /* ------------------------------------------------------------------ */
    /* internals                                                           */
    /* ------------------------------------------------------------------ */

    private int chunkIndex(short key) {
        // Arrays.binarySearch on unsigned keys
        int lo = 0, hi = size - 1;
        int target = key & 0xffff;
        while (lo <= hi) {
            int mid = (lo + hi) >>> 1;
            int mk = keys[mid] & 0xffff;
            if (mk < target) {
                lo = mid + 1;
            } else if (mk > target) {
                hi = mid - 1;
            } else {
                return mid;
            }
        }
        return -(lo + 1);
    }

    private void insertChunk(int at, short key, Container c) {
        if (size == keys.length) {
            int cap = Math.max(4, keys.length * 2);
            keys = Arrays.copyOf(keys, cap);
            containers = Arrays.copyOf(containers, cap);
        }
        System.arraycopy(keys, at, keys, at + 1, size - at);
        System.arraycopy(containers, at, containers, at + 1, size - at);
        keys[at] = key;
        containers[at] = c;
        size++;
    }

    private void append(int unsignedKey, Container c) {
        if (size == keys.length) {
            int cap = Math.max(4, keys.length * 2);
            keys = Arrays.copyOf(keys, cap);
            containers = Arrays.copyOf(containers, cap);
        }
        keys[size] = (short) unsignedKey;
        containers[size] = c;
        size++;
    }

    /* ------------------------------------------------------------------ */
    /* containers                                                          */
    /* ------------------------------------------------------------------ */

    abstract static class Container implements Cloneable {
        int cardinality;

        abstract boolean contains(int pos); // pos in [0,65535]

        abstract short last();

        abstract Container add(int pos);

        abstract Container remove(int pos);

        abstract Container or(Container o);

        abstract Container and(Container o);

        /** this \\ o */
        abstract Container andNot(Container o);

        abstract short[] values();

        abstract long byteSize();

        abstract Container copy();

        static ArrayContainer toArray(Container c) {
            if (c instanceof ArrayContainer) {
                return (ArrayContainer) c;
            }
            short[] v = c.values();
            return new ArrayContainer(v, v.length);
        }

        static Container pick(short[] values, int card) {
            if (card <= ARRAY_MAX) {
                return new ArrayContainer(values, card);
            }
            long[] words = new long[1024];
            for (int i = 0; i < card; i++) {
                int p = values[i] & 0xffff;
                words[p >>> 6] |= 1L << (p & 63);
            }
            return new BitmapContainer(words, card);
        }
    }

    static final class ArrayContainer extends Container implements Cloneable {
        short[] values; // sorted unique

        ArrayContainer(short[] values, int cardinality) {
            this.values = values;
            this.cardinality = cardinality;
        }

        @Override
        Container copy() {
            return new ArrayContainer(values.clone(), cardinality);
        }

        @Override
        boolean contains(int pos) {
            return unsignedBinarySearch(values, 0, cardinality, (short) pos) >= 0;
        }

        /** Binary search treating shorts as UNSIGNED 16-bit positions. */
        static int unsignedBinarySearch(short[] a, int from, int len, short key) {
            int target = key & 0xffff;
            int low = from;
            int high = from + len - 1;
            while (low <= high) {
                int mid = (low + high) >>> 1;
                int midVal = a[mid] & 0xffff;
                if (midVal < target) {
                    low = mid + 1;
                } else if (midVal > target) {
                    high = mid - 1;
                } else {
                    return mid;
                }
            }
            return -(low + 1);
        }

        @Override
        short last() {
            return values[cardinality - 1];
        }

        @Override
        Container add(int pos) {
            short p = (short) pos;
            int idx = unsignedBinarySearch(values, 0, cardinality, p);
            if (idx >= 0) {
                return this;
            }
            int at = -idx - 1;
            if (cardinality < values.length) {
                System.arraycopy(values, at, values, at + 1, cardinality - at);
                values[at] = p;
                cardinality++;
                if (cardinality > ARRAY_MAX) {
                    return toBitmap();
                }
                return this;
            }
            short[] nv = Arrays.copyOf(values, cardinality + 1);
            System.arraycopy(nv, at, nv, at + 1, cardinality - at);
            nv[at] = p;
            return pick(nv, cardinality + 1);
        }

        private BitmapContainer toBitmap() {
            long[] words = new long[1024];
            for (int i = 0; i < cardinality; i++) {
                int p = values[i] & 0xffff;
                words[p >>> 6] |= 1L << (p & 63);
            }
            return new BitmapContainer(words, cardinality);
        }

        @Override
        Container remove(int pos) {
            short p = (short) pos;
            int idx = unsignedBinarySearch(values, 0, cardinality, p);
            if (idx < 0) {
                return this;
            }
            short[] nv = new short[cardinality - 1];
            System.arraycopy(values, 0, nv, 0, idx);
            System.arraycopy(values, idx + 1, nv, idx, cardinality - idx - 1);
            return new ArrayContainer(nv, cardinality - 1);
        }

        @Override
        Container or(Container o) {
            if (o instanceof ArrayContainer) {
                ArrayContainer a = (ArrayContainer) o;
                short[] merged = new short[Math.min(CHUNK_SIZE, cardinality + a.cardinality)];
                int i = 0, j = 0, n = 0;
                while (i < cardinality || j < a.cardinality) {
                    int va = i < cardinality ? values[i] & 0xffff : Integer.MAX_VALUE;
                    int vb = j < a.cardinality ? a.values[j] & 0xffff : Integer.MAX_VALUE;
                    if (va == vb) {
                        merged[n++] = (short) va;
                        i++;
                        j++;
                    } else if (va < vb) {
                        merged[n++] = (short) va;
                        i++;
                    } else {
                        merged[n++] = (short) vb;
                        j++;
                    }
                }
                return pick(merged, n);
            }
            // bitmap OR: delegate symmetrically
            return o.or(this);
        }

        @Override
        Container and(Container o) {
            if (o instanceof BitmapContainer) {
                BitmapContainer b = (BitmapContainer) o;
                short[] out = new short[Math.min(cardinality, o.cardinality)];
                int n = 0;
                for (int i = 0; i < cardinality; i++) {
                    int p = values[i] & 0xffff;
                    if (b.contains(p)) {
                        out[n++] = values[i];
                    }
                }
                return new ArrayContainer(out, n);
            }
            ArrayContainer a = (ArrayContainer) o;
            short[] out = new short[Math.min(cardinality, a.cardinality)];
            int i = 0, j = 0, n = 0;
            while (i < cardinality && j < a.cardinality) {
                int va = values[i] & 0xffff;
                int vb = a.values[j] & 0xffff;
                if (va == vb) {
                    out[n++] = (short) va;
                    i++;
                    j++;
                } else if (va < vb) {
                    i++;
                } else {
                    j++;
                }
            }
            return new ArrayContainer(out, n);
        }

        @Override
        Container andNot(Container o) {
            short[] out = new short[cardinality];
            int n = 0;
            for (int i = 0; i < cardinality; i++) {
                int p = values[i] & 0xffff;
                if (!o.contains(p)) {
                    out[n++] = values[i];
                }
            }
            return new ArrayContainer(out, n);
        }

        @Override
        short[] values() {
            return Arrays.copyOf(values, cardinality);
        }

        @Override
        long byteSize() {
            return 2L * cardinality + 16L; // data + rough object header
        }
    }

    static final class BitmapContainer extends Container implements Cloneable {
        long[] words; // always length 1024

        BitmapContainer(long[] words, int cardinality) {
            this.words = words;
            this.cardinality = cardinality;
        }

        @Override
        Container copy() {
            return new BitmapContainer(words.clone(), cardinality);
        }

        static BitmapContainer full() {
            long[] w = new long[1024];
            Arrays.fill(w, ~0L);
            return new BitmapContainer(w, CHUNK_SIZE);
        }

        @Override
        boolean contains(int pos) {
            return (words[pos >>> 6] & (1L << (pos & 63))) != 0;
        }

        @Override
        short last() {
            for (int i = 1023; i >= 0; i--) {
                if (words[i] != 0) {
                    return (short) ((i << 6) + 63 - Long.numberOfLeadingZeros(words[i]));
                }
            }
            throw new IllegalStateException("empty container");
        }

        @Override
        Container add(int pos) {
            int wi = pos >>> 6;
            long bit = 1L << (pos & 63);
            if ((words[wi] & bit) == 0) {
                words[wi] |= bit;
                cardinality++;
            }
            return this;
        }

        @Override
        Container remove(int pos) {
            int wi = pos >>> 6;
            long bit = 1L << (pos & 63);
            if ((words[wi] & bit) != 0) {
                words[wi] &= ~bit;
                cardinality--;
            }
            if (cardinality <= ARRAY_MAX) {
                short[] vals = new short[cardinality];
                int n = 0;
                for (int i = 0; i < 1024; i++) {
                    long x = words[i];
                    while (x != 0) {
                        vals[n++] = (short) ((i << 6) + Long.numberOfTrailingZeros(x));
                        x &= x - 1;
                    }
                }
                return new ArrayContainer(vals, cardinality);
            }
            return this;
        }

        @Override
        Container or(Container o) {
            long[] w = words.clone();
            int card;
            if (o instanceof BitmapContainer) {
                BitmapContainer b = (BitmapContainer) o;
                for (int i = 0; i < 1024; i++) {
                    w[i] |= b.words[i];
                }
                card = 0;
                for (long x : w) {
                    card += Long.bitCount(x);
                }
            } else {
                ArrayContainer a = (ArrayContainer) o;
                for (int i = 0; i < a.cardinality; i++) {
                    int p = a.values[i] & 0xffff;
                    w[p >>> 6] |= 1L << (p & 63);
                }
                card = cardinality;
                for (int i = 0; i < a.cardinality; i++) {
                    if (!contains(a.values[i] & 0xffff)) {
                        card++;
                    }
                }
            }
            if (card <= ARRAY_MAX) {
                short[] vals = new short[card];
                int n = 0;
                for (int i = 0; i < 1024; i++) {
                    long x = w[i];
                    while (x != 0) {
                        vals[n++] = (short) ((i << 6) + Long.numberOfTrailingZeros(x));
                        x &= x - 1;
                    }
                }
                return new ArrayContainer(vals, card);
            }
            return new BitmapContainer(w, card);
        }

        @Override
        Container and(Container o) {
            long[] w = new long[1024];
            if (o instanceof BitmapContainer) {
                BitmapContainer b = (BitmapContainer) o;
                for (int i = 0; i < 1024; i++) {
                    w[i] = words[i] & b.words[i];
                }
            } else {
                ArrayContainer a = (ArrayContainer) o;
                for (int i = 0; i < a.cardinality; i++) {
                    int p = a.values[i] & 0xffff;
                    if (contains(p)) {
                        w[p >>> 6] |= 1L << (p & 63);
                    }
                }
            }
            int card = 0;
            for (long x : w) {
                card += Long.bitCount(x);
            }
            if (card <= ARRAY_MAX) {
                short[] vals = new short[card];
                int n = 0;
                for (int i = 0; i < 1024; i++) {
                    long x = w[i];
                    while (x != 0) {
                        vals[n++] = (short) ((i << 6) + Long.numberOfTrailingZeros(x));
                        x &= x - 1;
                    }
                }
                return new ArrayContainer(vals, card);
            }
            return new BitmapContainer(w, card);
        }

        @Override
        Container andNot(Container o) {
            // this \ o, result intersected with nothing else; caller is responsible
            // for universe restriction when evaluating NOT
            long[] w = new long[1024];
            if (o instanceof BitmapContainer) {
                BitmapContainer b = (BitmapContainer) o;
                for (int i = 0; i < 1024; i++) {
                    w[i] = words[i] & ~b.words[i];
                }
            } else {
                // Start from this, clear array positions
                System.arraycopy(words, 0, w, 0, 1024);
                ArrayContainer a = (ArrayContainer) o;
                for (int i = 0; i < a.cardinality; i++) {
                    int p = a.values[i] & 0xffff;
                    w[p >>> 6] &= ~(1L << (p & 63));
                }
            }
            int card = 0;
            for (long x : w) {
                card += Long.bitCount(x);
            }
            if (card <= ARRAY_MAX) {
                short[] vals = new short[card];
                int n = 0;
                for (int i = 0; i < 1024; i++) {
                    long x = w[i];
                    while (x != 0) {
                        vals[n++] = (short) ((i << 6) + Long.numberOfTrailingZeros(x));
                        x &= x - 1;
                    }
                }
                return new ArrayContainer(vals, card);
            }
            return new BitmapContainer(w, card);
        }

        @Override
        short[] values() {
            short[] out = new short[cardinality];
            int n = 0;
            for (int i = 0; i < 1024; i++) {
                long x = words[i];
                while (x != 0) {
                    out[n++] = (short) ((i << 6) + Long.numberOfTrailingZeros(x));
                    x &= x - 1;
                }
            }
            return out;
        }

        @Override
        long byteSize() {
            return 8L * 1024 + 16L;
        }
    }
}
