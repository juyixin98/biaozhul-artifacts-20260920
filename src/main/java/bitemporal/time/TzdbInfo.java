package bitemporal.time;

import java.time.ZoneId;
import java.time.zone.ZoneRulesException;
import java.time.zone.ZoneRulesProvider;
import java.util.Locale;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;

/**
 * 时区数据库（IANA tzdb）版本信息。
 *
 * <p>JDK 把 IANA 时区数据库打进 {@code java.time}，版本号可通过公开 API
 * {@link ZoneRulesProvider#getVersions(String)} 取得（map 的键即 tzdb 版本，
 * 如 {@code 2026b}）。该方法在各 JDK 厂商版本上均可用，无需反射或
 * {@code --add-exports}。</p>
 */
public final class TzdbInfo {

    private TzdbInfo() {
    }

    /** 返回内置 IANA tzdb 版本（如 2026b）；无法识别时返回空。 */
    public static Optional<String> version() {
        try {
            return ZoneRulesProvider.getVersions("Asia/Shanghai").keySet().stream()
                    .filter(v -> v != null && !v.isBlank())
                    .findFirst();
        } catch (ZoneRulesException e) {
            return Optional.empty();
        }
    }

    /** JVM 默认时区标识，如 Asia/Shanghai。 */
    public static String defaultZoneId() {
        return ZoneId.systemDefault().getId();
    }

    /** 全部可用时区标识数量（用于记录数据规模，不做任何预约/考勤用途）。 */
    public static int availableZoneCount() {
        Set<String> ids = new TreeSet<>(ZoneRulesProvider.getAvailableZoneIds());
        return ids.size();
    }

    /** 简短的人类可读摘要，供启动日志与 info 响应使用。 */
    public static String summary() {
        return String.format(Locale.ROOT,
                "IANA tzdb version=%s (bundled with the running JDK), default zone=%s, zone count=%d",
                version().orElse("unknown"), defaultZoneId(), availableZoneCount());
    }
}
