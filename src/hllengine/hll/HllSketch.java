package hllengine.hll;

import hllengine.api.ApiException;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.zip.CRC32;

/**
 * HyperLogLog sketch (64-bit hashing, single precision p, byte-per-register
 * sparse-independent dense representation).
 *
 * <p>The estimate is an <b>approximation</b>, never an exact count. Every
 * result returned by the engine carries:
 * <ul>
 *   <li>{@code estimatedCardinality} &ndash; the point estimate;</li>
 *   <li>{@code nominalRelativeStandardError} &ndash; 1.04/&radic;m, the
 *       theoretical standard deviation of the estimator (an error scale, not a
 *       guarantee and not a measured error);</li>
 *   <li>{@code observedCount} &ndash; the exact number of inserted values,
 *       i.e. stream length with duplicates counted, distinct from the
 *       estimated cardinality.</li>
 * </ul>
 *
 * <p>Estimator: classic HyperLogLog with linear counting in the small range
 * ({@code E &le; 2.5m}, Flajolet et al. 2007). No large-range correction is
 * needed because the hash is 64 bits wide (bias becomes relevant far beyond
 * practical cardinalities).
 */
public final class HllSketch {

    // ------------------------------------------------------- binary wire format

    /** Version 1 binary format, fixed by this build. */
    public static final int FORMAT_VERSION = 1;
    static final byte[] MAGIC = {'H', 'L', 'L', '1'};

    private final HllConfig config;
    private final byte[] registers;
    private long observedCount;

    public HllSketch(HllConfig config) {
        this.config = config;
        this.registers = new byte[config.m()];
    }

    public HllConfig config() {
        return config;
    }

    public long observedCount() {
        return observedCount;
    }

    public boolean isEmpty() {
        return observedCount == 0;
    }

    /** Returns the (zeroed) register max rank value, for tests/diagnostics. */
    public byte registerValue(int index) {
        return registers[index];
    }

    /** Number of registers still at zero. */
    public int zeroRegisters() {
        int z = 0;
        for (byte r : registers) {
            if (r == 0) z++;
        }
        return z;
    }

    // ------------------------------------------------------------- insertions

    /** Inserts a JSON/Java scalar value (null, Boolean, Number, String). */
    public void offerValue(Object value) {
        offerBytes(ValueCoding.encode(value));
    }

    /** Fast path used by the benchmark: hashes a 64-bit long directly. */
    public void offerLong(long value) {
        offerHash(MurmurHash3.hash64(longBytes(value), config.seed()));
        observedCount++;
    }

    private static byte[] longBytes(long value) {
        byte[] out = new byte[9];
        out[0] = ValueCoding.TAG_LONG;
        for (int k = 0; k < 8; k++) {
            out[8 - k] = (byte) (value >>> (8 * k));
        }
        return out;
    }

    public void offerBytes(byte[] encoded) {
        offerHash(MurmurHash3.hash64(encoded, config.seed()));
        observedCount++;
    }

    /**
     * Updates the sketch with a 64-bit hash. The top {@code p} bits select the
     * register and the rank is 1 plus the number of leading zeroes of the
     * remaining (64&minus;p) bits, so rank ranges over [1, 64&minus;p+1].
     */
    void offerHash(long h1) {
        int p = config.precision();
        int index = (int) (h1 >>> (64 - p));
        long remaining = h1 << p;
        int rank = Long.numberOfLeadingZeros(remaining) + 1;
        if (registers[index] < rank) {
            registers[index] = (byte) rank;
        }
    }

    // ---------------------------------------------------------------- merging

    /**
     * Merges {@code other} into this sketch in place. Mergeable only when the
     * configurations match exactly (precision, hashId, seed); the binary
     * formats' versions need not be identical, but v1 is the only version
     * this build produces.
     *
     * @throws ApiException INCOMPATIBLE_CONFIG when configurations differ
     */
    public void merge(HllSketch other) {
        String diff = config.compatibilityDifference(other.config);
        if (diff != null) {
            throw new ApiException(ApiException.INCOMPATIBLE_CONFIG,
                    "cannot merge sketches: " + diff);
        }
        for (int k = 0; k < registers.length; k++) {
            if (other.registers[k] > registers[k]) {
                registers[k] = other.registers[k];
            }
        }
        observedCount += other.observedCount;
    }

    // --------------------------------------------------------------- estimate

    private double alpha() {
        int p = config.precision();
        int m = config.m();
        if (p == 4) return 0.673;
        if (p == 5) return 0.697;
        return 0.7213 / (1.0 + 1.079 / m);
    }

    /**
     * Computes the raw point estimate. Package-visible for tests; callers
     * normally use {@link #estimate()}.
     */
    double rawEstimate() {
        int m = config.m();
        double sum = 0.0;
        int zeros = 0;
        for (byte r : registers) {
            sum += 1.0 / (1L << r); // r==0 contributes 1.0
            if (r == 0) zeros++;
        }
        double raw = alpha() * (double) m * (double) m / sum;
        if (raw <= config.smallRangeThreshold() && zeros != 0) {
            // Small-range correction (linear counting).
            return m * Math.log((double) m / zeros);
        }
        return raw;
    }

    public HllEstimate estimate() {
        if (isEmpty()) {
            return new HllEstimate(0.0, 0L, config.relativeStandardError(), 0L, true,
                    config.m(), config.m());
        }
        double raw = rawEstimate();
        long rounded = Math.round(raw);
        return new HllEstimate(raw, rounded, config.relativeStandardError(),
                observedCount, false, zeroRegisters(), config.m());
    }

    // ------------------------------------------------------- serialization v1

    /**
     * Serializes to the v1 binary format:
     * <pre>
     *  magic "HLL1" (4) | version=1 (1) | flags=0 (1) | precision (1)
     *  | seed int32 BE (4) | hashIdLen uint16 BE (2) | hashId UTF-8
     *  | observedCount int64 BE (8) | registers[precision m]
     *  | crc32 uint32 BE (4), over all preceding bytes
     * </pre>
     */
    public byte[] serialize() {
        byte[] hashIdBytes = config.hashId().getBytes(StandardCharsets.UTF_8);
        int m = config.m();
        int len = MAGIC.length + 1 + 1 + 1 + 4 + 2 + hashIdBytes.length + 8 + m;
        byte[] out = new byte[len + 4];

        int pos = 0;
        System.arraycopy(MAGIC, 0, out, pos, MAGIC.length);
        pos += MAGIC.length;
        out[pos++] = FORMAT_VERSION;
        out[pos++] = 0; // flags
        out[pos++] = (byte) config.precision();
        writeIntBE(out, pos, config.seed());
        pos += 4;
        out[pos++] = (byte) ((hashIdBytes.length >>> 8) & 0xff);
        out[pos++] = (byte) (hashIdBytes.length & 0xff);
        System.arraycopy(hashIdBytes, 0, out, pos, hashIdBytes.length);
        pos += hashIdBytes.length;
        writeLongBE(out, pos, observedCount);
        pos += 8;
        System.arraycopy(registers, 0, out, pos, m);
        pos += m;

        CRC32 crc = new CRC32();
        crc.update(out, 0, pos);
        writeIntBE(out, pos, (int) crc.getValue());
        return out;
    }

    /** Serializes and base64-encodes (standard MIME base64 string). */
    public String serializeBase64() {
        return Base64.getEncoder().encodeToString(serialize());
    }

    /**
     * Full JSON export envelope. The {@code serialized} field is the canonical
     * portable form; the human-readable metadata is repeated alongside it.
     */
    public Map<String, Object> exportMap() {
        HllEstimate est = estimate();
        Map<String, Object> map = new LinkedHashMap<>();
        map.put("kind", "HllSketch");
        map.put("formatVersion", FORMAT_VERSION);
        map.put("config", config.toMap());
        map.put("observedCount", observedCount);
        map.put("estimatedCardinality", est.estimatedCardinality());
        map.put("estimatedCardinalityRaw", est.rawEstimate());
        map.put("nominalRelativeStandardError", est.relativeStandardError());
        map.put("serialized", serializeBase64());
        return map;
    }

    /**
     * Deserializes the v1 binary format, validating every fixed field and the
     * trailing checksum. Any malformed input raises {@link ApiException} with
     * code {@code BAD_FORMAT}.
     */
    public static HllSketch deserialize(byte[] data) {
        if (data == null) {
            throw bad("sketch bytes are missing");
        }
        int minLen = MAGIC.length + 1 + 1 + 1 + 4 + 2 + 0 + 8 + 0 + 4;
        if (data.length < minLen) {
            throw bad("sketch is truncated: length " + data.length + " < minimum " + minLen);
        }
        int pos = 0;
        for (byte b : MAGIC) {
            if (data[pos++] != b) {
                throw bad("wrong magic bytes: expected \"HLL1\"");
            }
        }
        int version = data[pos++] & 0xff;
        if (version != FORMAT_VERSION) {
            throw bad("unsupported format version " + version + "; expected " + FORMAT_VERSION);
        }
        int flags = data[pos++] & 0xff;
        if (flags != 0) {
            throw bad("unknown flag bits 0x" + Integer.toHexString(flags) + "; expected 0");
        }
        int p = data[pos++] & 0xff;
        if (p < HllConfig.MIN_PRECISION || p > HllConfig.MAX_PRECISION) {
            throw bad("precision " + p + " is outside the supported range ["
                    + HllConfig.MIN_PRECISION + ", " + HllConfig.MAX_PRECISION + "]");
        }
        int seed = readIntBE(data, pos);
        pos += 4;
        int idLen = ((data[pos] & 0xff) << 8) | (data[pos + 1] & 0xff);
        pos += 2;
        if (idLen <= 0 || pos + idLen + 8 > data.length - 4) {
            throw bad("invalid hashId length " + idLen + " or truncated payload");
        }
        String hashId = new String(data, pos, idLen, StandardCharsets.UTF_8);
        pos += idLen;
        if (!MurmurHash3.HASH_ID.equals(hashId)) {
            throw bad("unknown hashId \"" + hashId + "\"; expected " + MurmurHash3.HASH_ID);
        }
        long count = readLongBE(data, pos);
        pos += 8;
        if (count < 0) {
            throw bad("observedCount must be non-negative, got " + count);
        }
        int m = 1 << p;
        if (pos + m + 4 != data.length) {
            throw bad("payload length does not match precision: expected "
                    + (pos + m + 4) + " bytes but got " + data.length);
        }
        int maxRank = 64 - p + 1;
        for (int k = 0; k < m; k++) {
            int r = data[pos + k] & 0xff;
            if (r > maxRank) {
                throw bad("register " + k + " holds rank " + r
                        + " above the legal maximum " + maxRank + " for precision " + p);
            }
        }
        int givenCrc = readIntBE(data, pos + m);
        CRC32 crc = new CRC32();
        crc.update(data, 0, pos + m);
        if (givenCrc != (int) crc.getValue()) {
            throw bad("checksum mismatch: payload is corrupt or was modified");
        }

        HllSketch sketch = new HllSketch(HllConfig.of(p, seed));
        System.arraycopy(data, pos, sketch.registers, 0, m);
        sketch.observedCount = count;
        return sketch;
    }

    public static HllSketch deserializeBase64(String base64) {
        if (base64 == null || base64.isEmpty()) {
            throw bad("base64 sketch payload is empty");
        }
        byte[] bytes;
        try {
            bytes = Base64.getDecoder().decode(base64);
        } catch (IllegalArgumentException iae) {
            throw bad("sketch payload is not valid base64: " + iae.getMessage());
        }
        return deserialize(bytes);
    }

    /**
     * Imports an export envelope {@code {"config": {...}, "serialized": "..."}}
     * or a bare base64 string. When the envelope repeats the config, it must
     * agree with the config embedded in the serialized bytes.
     */
    public static HllSketch importFrom(Object value) {
        if (value instanceof String) {
            return deserializeBase64((String) value);
        }
        if (!(value instanceof Map)) {
            throw bad("sketch import must be a base64 string or an export envelope object");
        }
        Map<?, ?> envelope = (Map<?, ?>) value;
        Object serialized = envelope.get("serialized");
        if (!(serialized instanceof String)) {
            throw bad("envelope is missing the string field \"serialized\"");
        }
        HllSketch sketch = deserializeBase64((String) serialized);
        Object cfg = envelope.get("config");
        if (cfg != null) {
            if (!(cfg instanceof Map)) {
                throw bad("envelope field \"config\" must be an object");
            }
            Object pObj = ((Map<?, ?>) cfg).get("precision");
            Object seedObj = ((Map<?, ?>) cfg).get("seed");
            Object hashObj = ((Map<?, ?>) cfg).get("hashId");
            if (pObj != null && ((Number) pObj).intValue() != sketch.config.precision()) {
                throw new ApiException(ApiException.INCOMPATIBLE_CONFIG,
                        "envelope precision " + pObj + " does not match serialized precision "
                                + sketch.config.precision());
            }
            if (seedObj != null && ((Number) seedObj).intValue() != sketch.config.seed()) {
                throw new ApiException(ApiException.INCOMPATIBLE_CONFIG,
                        "envelope seed " + seedObj + " does not match serialized seed "
                                + sketch.config.seed());
            }
            if (hashObj != null && !String.valueOf(hashObj).equals(sketch.config.hashId())) {
                throw new ApiException(ApiException.INCOMPATIBLE_CONFIG,
                        "envelope hashId \"" + hashObj + "\" does not match serialized \""
                                + sketch.config.hashId() + "\"");
            }
        }
        Object fv = envelope.get("formatVersion");
        if (fv != null && ((Number) fv).intValue() != FORMAT_VERSION) {
            throw bad("envelope formatVersion " + fv + " is not supported");
        }
        return sketch;
    }

    private static ApiException bad(String message) {
        return new ApiException(ApiException.BAD_FORMAT, message);
    }

    private static void writeIntBE(byte[] b, int off, int v) {
        b[off] = (byte) (v >>> 24);
        b[off + 1] = (byte) (v >>> 16);
        b[off + 2] = (byte) (v >>> 8);
        b[off + 3] = (byte) v;
    }

    private static int readIntBE(byte[] b, int off) {
        return ((b[off] & 0xff) << 24) | ((b[off + 1] & 0xff) << 16)
                | ((b[off + 2] & 0xff) << 8) | (b[off + 3] & 0xff);
    }

    private static void writeLongBE(byte[] b, int off, long v) {
        for (int k = 0; k < 8; k++) {
            b[off + k] = (byte) (v >>> (56 - 8 * k));
        }
    }

    private static long readLongBE(byte[] b, int off) {
        long v = 0;
        for (int k = 0; k < 8; k++) {
            v = (v << 8) | (b[off + k] & 0xffL);
        }
        return v;
    }
}
