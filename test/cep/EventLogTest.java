package cep;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

/**
 * 预写日志测试：追加/重放的往返一致性、末尾半行截断恢复、中间损坏拒绝启动。
 */
public class EventLogTest {

    private Path tempDir() throws Exception {
        return Files.createTempDirectory("cep-wal-test-");
    }

    private static Event ev(String type, String entity, long ts, long seq) {
        return new Event(type, entity, ts, seq);
    }

    public void testAppendAndReplayRoundTrip() throws Exception {
        Path dir = tempDir();
        List<Event> batch1 = List.of(
                ev("A", "x", 0, 0),
                ev("A", "x", 10, 1),
                ev("B", "x", 20, 2));
        List<Event> batch2 = List.of(ev("C", "x", 30, 3));

        try (EventLog log = new EventLog(dir)) {
            log.appendBatch(batch1);
            log.appendBatch(batch2);
        }

        try (EventLog log = new EventLog(dir)) {
            List<List<Event>> got = log.replay();
            TestRunner.checkEq(got.size(), 2, "应重放出 2 个批次");
            TestRunner.checkEq(got.get(0).size(), 3, "第一批 3 个事件");
            TestRunner.checkEq(got.get(1).size(), 1, "第二批 1 个事件");
            TestRunner.checkEq(got.get(0).get(2).type, "B", "事件类型往返一致");
            TestRunner.checkEq(got.get(0).get(2).seq, 2L, "输入序号往返一致");
            TestRunner.checkEq(got.get(1).get(0).timestamp, 30L, "时间戳往返一致");
            TestRunner.checkEq(got.get(1).get(0).entityId, "x", "实体标识往返一致");
        }
    }

    /** 崩溃时最后一行只写了一半：重放应截断该行并恢复前面的完整批次。 */
    public void testTornTailLineIsTruncated() throws Exception {
        Path dir = tempDir();
        try (EventLog log = new EventLog(dir)) {
            log.appendBatch(List.of(ev("A", "x", 0, 0)));
            log.appendBatch(List.of(ev("B", "x", 10, 1)));
        }
        Path file = dir.resolve("event.log");
        String goodText = Files.readString(file, StandardCharsets.UTF_8);
        String tornRecord = "{\"events\":[{\"type\":\"C\",\"entityId\":\"x\",\"timestamp\":20,\"s";
        // 注意：必须用字符串拼接（good 读成 String），byte[] + String 会变成 [B@hash。
        Files.write(file, (goodText + tornRecord).getBytes(StandardCharsets.UTF_8));

        try (EventLog log = new EventLog(dir)) {
            List<List<Event>> got = log.replay();
            TestRunner.checkEq(got.size(), 2, "半行批次不算数，应恢复 2 个完整批次");
            // 截断后文件应当只含两个完整行
            List<String> lines = Files.readAllLines(file, StandardCharsets.UTF_8);
            TestRunner.checkEq(lines.size(), 2, "半行必须被物理截断");

            // 截断后新追加的批次应能正常衔接（文件状态可用）
            log.appendBatch(List.of(ev("C", "x", 20, 2)));
            List<List<Event>> again = log.replay();
            TestRunner.checkEq(again.size(), 3, "截断恢复后可继续追加并重放");
            TestRunner.checkEq(again.get(2).get(0).seq, 2L, "新批次内容正确");
        }
    }

    /** 日志中间行损坏（非末尾）：必须拒绝启动，绝不静默跳过。 */
    public void testCorruptMiddleLineRejectsStartup() throws Exception {
        Path dir = tempDir();
        try (EventLog log = new EventLog(dir)) {
            log.appendBatch(List.of(ev("A", "x", 0, 0)));
        }
        Path file = dir.resolve("event.log");
        String goodText = Files.readString(file, StandardCharsets.UTF_8);
        String corruptMiddle = "{这不是合法JSON\n";
        String laterGood = "{\"events\":[{\"type\":\"C\",\"entityId\":\"x\",\"timestamp\":20,\"seq\":1}]}\n";
        Files.write(file,
                (goodText + corruptMiddle + laterGood).getBytes(StandardCharsets.UTF_8));

        boolean threw = false;
        try (EventLog log = new EventLog(dir)) {
            log.replay();
        } catch (IllegalStateException ex) {
            threw = true;
        }
        TestRunner.check(threw, "中间行损坏必须抛出异常拒绝启动");
    }

    /** 空数据目录重放为空。 */
    public void testEmptyLogReplaysEmpty() throws Exception {
        Path dir = tempDir();
        try (EventLog log = new EventLog(dir)) {
            TestRunner.check(log.replay().isEmpty(), "新日志应重放出 0 个批次");
            log.appendBatch(List.of());
            TestRunner.checkEq(log.replay().size(), 1, "空批次记录也应保留（批次边界有意义）");
        }
    }

    /** reset 后日志清空，历史数据不再重放。 */
    public void testResetClearsLog() throws Exception {
        Path dir = tempDir();
        try (EventLog log = new EventLog(dir)) {
            log.appendBatch(List.of(ev("A", "x", 0, 0)));
            log.reset();
            TestRunner.check(log.replay().isEmpty(), "reset 后重放应为空");
            log.appendBatch(List.of(ev("B", "y", 5, 0)));
            List<List<Event>> got = log.replay();
            TestRunner.checkEq(got.size(), 1, "reset 后日志可重新使用");
            TestRunner.checkEq(got.get(0).get(0).entityId, "y", "新内容不夹带旧数据");
        }
    }

    /** fsync 后文件字节与内存写入内容一致（基础落盘校验）。 */
    public void testBytesAreFlushed() throws Exception {
        Path dir = tempDir();
        try (EventLog log = new EventLog(dir)) {
            log.appendBatch(List.of(ev("A", "x", 0, 0)));
        }
        Path file = dir.resolve("event.log");
        ByteArrayOutputStream all = new ByteArrayOutputStream();
        Files.copy(file, all);
        String content = all.toString(StandardCharsets.UTF_8);
        TestRunner.check(content.endsWith("\n"), "每条记录以换行结尾（NDJSON）");
        TestRunner.check(content.contains("\"seq\":0"), "记录中包含输入序号");
    }
}
