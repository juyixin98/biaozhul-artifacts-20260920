package com.example.timeout.tz;

import java.time.zone.ZoneRulesProvider;
import java.util.Set;

/**
 * 读取运行环境的 IANA 时区数据库（tzdb）版本，例如 {@code 2026b}。
 *
 * <p>截止时间换算依赖时区规则（尤其 DST），因此必须把 tzdb 版本随服务信息一起记录、上报，
 * 便于跨进程/跨机器核对“同一本地时刻为何被解析成这一 UTC 瞬时”。
 */
public final class TzdbInfo {

    private TzdbInfo() {
    }

    /** JDK 内置 tzdb 用同一个版本字符串注册所有区域；依次尝试常见固定区域名获取它。 */
    public static String version() {
        for (String zone : new String[]{"UTC", "Etc/UTC", "Z"}) {
            Set<String> versions = ZoneRulesProvider.getVersions(zone).keySet();
            if (!versions.isEmpty()) {
                return versions.iterator().next();
            }
        }
        return "unknown";
    }

    public static String javaVersion() {
        return System.getProperty("java.version", "unknown");
    }
}
