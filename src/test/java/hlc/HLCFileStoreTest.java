package hlc;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;

public class HLCFileStoreTest {

    @Test("save then load restores identical timestamps")
    void roundTrip() throws Exception {
        Path file = Files.createTempDirectory("hlc-fs-").resolve("state.properties");
        try {
            HLCFileStore store = new HLCFileStore(file);
            Map<String, HLCTimestamp> in = Map.of(
                    "alice", new HLCTimestamp(1_000, 3),
                    "bob", new HLCTimestamp(2_000, 1));
            store.save(in);
            Map<String, HLCTimestamp> out = store.load();
            TestRunner.assertEquals(in.get("alice"), out.get("alice"), "alice restored");
            TestRunner.assertEquals(in.get("bob"), out.get("bob"), "bob restored");
            TestRunner.assertTrue(Files.readString(file).contains("tzdb="),
                    "tzdb version is recorded in the file");
        } finally {
            Files.deleteIfExists(file);
        }
    }

    @Test("missing file loads as empty (fresh start)")
    void missingFile() {
        String tmp = System.getProperty("java.io.tmpdir");
        HLCFileStore store = new HLCFileStore(
                Path.of(tmp, "hlc-does-not-exist-" + System.nanoTime()));
        TestRunner.assertTrue(store.load().isEmpty(), "no state yet");
    }

    @Test("an incompatible format version is refused")
    void badVersion() throws Exception {
        Path file = Files.createTempDirectory("hlc-fs-bad-").resolve("state.properties");
        try {
            Files.writeString(file, "version=999\nnode.a=1:1\n");
            TestRunner.assertThrows(HLCException.class, () -> new HLCFileStore(file).load());
        } finally {
            Files.deleteIfExists(file);
        }
    }
}
