package dev.intervals.tz;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.ZoneId;
import java.time.zone.ZoneRulesProvider;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 收集时区数据库版本信息。
 *
 * <p>记录两个来源：
 * <ul>
 *   <li>JVM 内置 tzdb（{@link ZoneRulesProvider}），Java 运行时自带；</li>
 *   <li>操作系统 tzdata（Linux {@code zoneinfo}），若可读取。</li>
 * </ul>
 *
 * <p>本服务只做区间集合代数，不做时区换算；该信息仅用于明确“数据版本基线”。
 */
public final class TzVersion {

    private static final Pattern VERSION_LINE = Pattern.compile("version\\s+(\\S+)");

    private TzVersion() {
    }

    public static Map<String, Object> info() {
        Map<String, Object> info = new LinkedHashMap<>();
        info.put("jvmTzdbVersion", jvmVersion());
        info.put("javaVersion", System.getProperty("java.version"));
        info.put("jvmDefaultZone", ZoneId.systemDefault().getId());
        info.put("osTzdataVersion", osVersion());
        info.put("zoneIdCount", zoneIds().size());
        return info;
    }

    /** JVM 内置 tzdb 版本，如 2026b；不可用时返回 "unknown"。 */
    public static String jvmVersion() {
        TreeSet<String> versions = new TreeSet<>(ZoneRulesProvider.getVersions("Etc/UTC").keySet());
        return versions.isEmpty() ? "unknown" : versions.last();
    }

    /**
     * 操作系统 tzdata 版本。依次尝试 {@code /usr/share/zoneinfo/+VERSION}
     * 与 tzdata.zi 中的 version 行；不可用返回 "unknown"。
     */
    public static String osVersion() {
        Path plusVersion = Path.of("/usr/share/zoneinfo/+VERSION");
        try {
            if (Files.isRegularFile(plusVersion)) {
                return Files.readString(plusVersion).trim();
            }
        } catch (IOException ignored) {
            // 继续尝试 tzdata.zi
        }
        Path zi = Path.of("/usr/share/zoneinfo/tzdata.zi");
        try {
            if (Files.isRegularFile(zi)) {
                for (String line : Files.readAllLines(zi)) {
                    Matcher m = VERSION_LINE.matcher(line);
                    if (m.find()) {
                        return m.group(1);
                    }
                }
            }
        } catch (IOException ignored) {
            // 落到 unknown
        }
        return "unknown";
    }

    public static Set<String> zoneIds() {
        return ZoneId.getAvailableZoneIds();
    }
}
