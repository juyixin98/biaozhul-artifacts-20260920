package hlc;

public class HLCTimestampTest {

    @Test("parse reproduces the canonical form")
    void parseRoundTrip() {
        HLCTimestamp t = new HLCTimestamp(1_700_000_000_000_000L, 42L);
        HLCTimestamp p = HLCTimestamp.parse(t.toString());
        TestRunner.assertEquals(t, p, "parse(toString) must be identity");
        TestRunner.assertEquals("1700000000000000:42", t.toString(), "canonical wire form");
    }

    @Test("ordering is lexicographic on (l, c)")
    void ordering() {
        TestRunner.assertLess(new HLCTimestamp(1, 0), new HLCTimestamp(1, 1), "same l, c grows");
        TestRunner.assertLess(new HLCTimestamp(1, 99), new HLCTimestamp(2, 0), "l dominates");
        TestRunner.assertEquals(0, new HLCTimestamp(5, 5).compareTo(new HLCTimestamp(5, 5)),
                "equal compares 0");
    }

    @Test("malformed timestamps are rejected at the boundary")
    void rejectsMalformed() {
        String[] bad = {"", "123", ":1", "1:", "a:b", "1:-2", "-1:2"};
        for (String s : bad) {
            TestRunner.assertThrows(HLCException.class, () -> HLCTimestamp.parse(s));
        }
        TestRunner.assertThrows(HLCException.class, () -> new HLCTimestamp(-1, 0));
        TestRunner.assertThrows(HLCException.class, () -> new HLCTimestamp(0, -1));
    }
}
