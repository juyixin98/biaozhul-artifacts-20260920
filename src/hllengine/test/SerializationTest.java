package hllengine.test;

import hllengine.api.ApiException;
import hllengine.hll.HllConfig;
import hllengine.hll.HllSketch;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.Map;

public final class SerializationTest implements TestRunner.Suite {

    @Override
    public void register(TestRunner.Registry r) {
        r.add("ser.emptySketchRoundTrips", this::emptyRoundTrip);
        r.add("ser.populatedSketchRoundTripsExactly", this::populatedRoundTrip);
        r.add("ser.mergedSketchSurvivesExportImport", this::mergedRoundTrip);
        r.add("ser.rejectsBadBase64", this::badBase64);
        r.add("ser.rejectsNull", this::nullInput);
        r.add("ser.rejectsWrongMagic", this::wrongMagic);
        r.add("ser.rejectsTruncatedPayload", this::truncated);
        r.add("ser.rejectsUnsupportedVersion", this::badVersion);
        r.add("ser.rejectsNonzeroFlags", this::badFlags);
        r.add("ser.rejectsOutOfRangePrecision", this::badPrecision);
        r.add("ser.rejectsUnknownHashId", this::badHashId);
        r.add("ser.rejectsCorruptChecksum", this::badChecksum);
        r.add("ser.rejectsRegisterRankAboveMax", this::badRank);
        r.add("ser.rejectsNegativeObservedCount", this::negativeCount);
        r.add("ser.envelopeConfigMismatchRejected", this::envelopeMismatch);
        r.add("ser.jsonExportIsSelfDescribing", this::envelopeShape);
    }

    private HllSketch build(long distinct) {
        HllSketch s = new HllSketch(HllConfig.of(12, 0));
        for (long k = 0; k < distinct; k++) s.offerLong(k);
        return s;
    }

    private void assertRejected(TestRunner.Assert a, byte[] bytes, String what) {
        boolean threw = false;
        String code = null;
        try {
            HllSketch.deserialize(bytes);
        } catch (ApiException ae) {
            threw = true;
            code = ae.code();
        }
        a.check(threw, "malformed sketch must be rejected: " + what);
        a.eq(code, ApiException.BAD_FORMAT, "error code for " + what);
    }

    private void emptyRoundTrip(TestRunner.Assert a) {
        HllSketch s = new HllSketch(HllConfig.of(12, 0));
        HllSketch back = HllSketch.deserialize(s.serialize());
        a.eq(back.estimate().estimatedCardinality(), 0L, "empty sketch restored");
        a.eq(back.config(), s.config(), "config restored");
        a.eq(back.observedCount(), 0L, "count restored");
    }

    private void populatedRoundTrip(TestRunner.Assert a) {
        HllSketch s = build(5_000);
        // Duplicate some values too.
        for (long k = 0; k < 500; k++) s.offerLong(k);
        HllSketch back = HllSketch.deserialize(s.serialize());
        a.eq(back.config(), s.config(), "config preserved");
        a.eq(back.observedCount(), s.observedCount(), "observedCount preserved");
        a.eq(back.estimate().rawEstimate(), s.estimate().rawEstimate(),
                "registers preserved -> identical raw estimate");
        a.eq(back.estimate().estimatedCardinality(), s.estimate().estimatedCardinality(),
                "rounded estimate identical");
    }

    private void mergedRoundTrip(TestRunner.Assert a) {
        HllSketch merged = new HllSketch(HllConfig.of(11, 7));
        for (int part = 0; part < 3; part++) {
            HllSketch p = new HllSketch(HllConfig.of(11, 7));
            for (long k = part * 1000L; k < (part + 1) * 1000L; k++) p.offerLong(k);
            merged.merge(p);
        }
        String exported = merged.serializeBase64();
        HllSketch back = HllSketch.deserializeBase64(exported);
        a.eq(back.config(), merged.config(), "p=11 seed=7 config round trip");
        a.eq(back.estimate().rawEstimate(), merged.estimate().rawEstimate(), "merged registers restored");
        a.approx(back.estimate().estimatedCardinality(), 3000, 0.08, "merged estimate ~3000");
    }

    private void badBase64(TestRunner.Assert a) {
        boolean threw = false;
        try {
            HllSketch.deserializeBase64("!!!not-base64!!!");
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.BAD_FORMAT, "bad base64 code");
        }
        a.check(threw, "invalid base64 rejected");

        threw = false;
        try {
            HllSketch.deserializeBase64("");
        } catch (ApiException ae) {
            threw = true;
        }
        a.check(threw, "empty base64 rejected");
    }

    private void nullInput(TestRunner.Assert a) {
        assertRejected(a, null, "null bytes");
    }

    private byte[] validBytes() {
        return build(100).serialize();
    }

    private void wrongMagic(TestRunner.Assert a) {
        byte[] b = validBytes();
        b[0] = 'X';
        assertRejected(a, b, "wrong magic");
    }

    private void truncated(TestRunner.Assert a) {
        byte[] b = validBytes();
        assertRejected(a, java.util.Arrays.copyOf(b, b.length - 5), "trailing truncation");
        assertRejected(a, java.util.Arrays.copyOf(b, 10), "very short input");
        assertRejected(a, new byte[0], "zero length");
    }

    private void badVersion(TestRunner.Assert a) {
        byte[] b = validBytes();
        b[4] = 99;
        assertRejected(a, b, "unknown version");
        b[4] = 2;
        assertRejected(a, b, "future version");
    }

    private void badFlags(TestRunner.Assert a) {
        byte[] b = validBytes();
        b[5] = 1;
        assertRejected(a, b, "flags nonzero");
    }

    private void badPrecision(TestRunner.Assert a) {
        byte[] b = validBytes();
        b[6] = 3;
        assertRejected(a, b, "precision too small");
        b[6] = 19;
        assertRejected(a, b, "precision too large");
        b[6] = 30;
        assertRejected(a, b, "precision nonsense");
    }

    private void badHashId(TestRunner.Assert a) {
        byte[] b = validBytes();
        // Layout before hashId: magic(4)+version(1)+flags(1)+precision(1)+seed(4)+len(2) = 13.
        int idLen = ((b[11] & 0xff) << 8) | (b[12] & 0xff);
        a.eq(idLen, "MURMUR3_X64_128".length(), "hashId length as expected");
        int idStart = 13;
        b[idStart] = 'X';
        // Fix checksum so the failure is specifically the hashId, not CRC.
        fixCrc(b);
        assertRejected(a, b, "unknown hashId");

        // Corrupt the declared length -> truncation-style failure.
        byte[] b2 = validBytes();
        b2[11] = 0x7f;
        b2[12] = (byte) 0xff;
        assertRejected(a, b2, "implausible hashId length");
    }

    private void badChecksum(TestRunner.Assert a) {
        byte[] b = validBytes();
        int last = b.length - 1;
        b[last] ^= (byte) 0xff;
        assertRejected(a, b, "flipped checksum byte");

        byte[] b2 = validBytes();
        // Flip a register byte; CRC must catch it.
        int regStart = b2.length - 4 - 4096;
        b2[regStart + 10] ^= 0x01;
        assertRejected(a, b2, "flipped register byte");
    }

    private void badRank(TestRunner.Assert a) {
        byte[] b = validBytes();
        int regStart = b.length - 4 - 4096;
        b[regStart] = 64; // legal max rank for p=12 is 53
        fixCrc(b);
        assertRejected(a, b, "rank 64 above max 53 for p=12");
    }

    private void negativeCount(TestRunner.Assert a) {
        byte[] b = validBytes();
        // observedCount is the 8 bytes right before the register region.
        int regStart = b.length - 4 - 4096;
        int countStart = regStart - 8;
        b[countStart] = (byte) 0x80;
        fixCrc(b);
        assertRejected(a, b, "negative observedCount");
    }

    private void fixCrc(byte[] b) {
        int regStart = b.length - 4 - 4096;
        int covered = regStart + 4096;
        java.util.zip.CRC32 crc = new java.util.zip.CRC32();
        crc.update(b, 0, covered);
        int v = (int) crc.getValue();
        b[covered] = (byte) (v >>> 24);
        b[covered + 1] = (byte) (v >>> 16);
        b[covered + 2] = (byte) (v >>> 8);
        b[covered + 3] = (byte) v;
    }

    private void envelopeMismatch(TestRunner.Assert a) {
        HllSketch s = build(50);
        Map<String, Object> envelope = new LinkedHashMap<>(s.exportMap());
        @SuppressWarnings("unchecked")
        Map<String, Object> cfg = (Map<String, Object>) envelope.get("config");
        cfg.put("precision", 14); // lies about the embedded config
        boolean threw = false;
        try {
            HllSketch.importFrom(envelope);
        } catch (ApiException ae) {
            threw = true;
            a.eq(ae.code(), ApiException.INCOMPATIBLE_CONFIG, "mismatch code");
        }
        a.check(threw, "envelope/serialized config mismatch rejected");
    }

    private void envelopeShape(TestRunner.Assert a) {
        HllSketch s = build(10);
        Map<String, Object> out = s.exportMap();
        a.eq(out.get("kind"), "HllSketch", "kind");
        a.eq(out.get("formatVersion"), 1, "version");
        a.check(out.get("serialized") instanceof String, "serialized is base64 string");
        @SuppressWarnings("unchecked")
        Map<String, Object> cfg = (Map<String, Object>) out.get("config");
        a.eq(cfg.get("hashId"), "MURMUR3_X64_128", "hashId surfaced");
        // Bare base64 import and envelope import must agree.
        String b64 = (String) out.get("serialized");
        HllSketch fromBare = HllSketch.importFrom(b64);
        HllSketch fromEnv = HllSketch.importFrom(out);
        a.eq(fromBare.estimate().rawEstimate(), fromEnv.estimate().rawEstimate(),
                "bare and envelope import agree");
    }
}
