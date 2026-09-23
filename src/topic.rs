//! MQTT 主题名校验与通配符订阅匹配（MQTT 3.1.1 §4.7）。
//!
//! - 主题名（PUBLISH）：区分大小写、不得以 `\0` 结尾、不得含通配符 `+`/`#`。
//! - 主题过滤器（SUBSCRIBE）：`+` 匹配恰好一级，`#` 只能是最后一级且匹配其后所有层级。
//! - 以 `$` 开头的主题不被通配符过滤器匹配（§4.7.2，服务端内部主题语义）。

/// 校验 PUBLISH 主题名（§4.7.3）。
pub fn is_valid_topic_name(topic: &str) -> bool {
    if topic.is_empty() {
        return false;
    }
    if topic.contains(['+', '#']) {
        return false;
    }
    !contains_nul(topic)
}

/// 校验 SUBSCRIBE 主题过滤器（§4.7.1）。
pub fn is_valid_topic_filter(filter: &str) -> bool {
    if filter.is_empty() || contains_nul(filter) {
        return false;
    }

    let levels: Vec<&str> = filter.split('/').collect();
    for (i, level) in levels.iter().enumerate() {
        if level.contains('#') {
            // `#` 必须独占整级，且只能出现在最后一级。
            if *level != "#" || i != levels.len() - 1 {
                return false;
            }
        } else if level.contains('+') {
            // `+` 必须独占整级（"a+b" 非法）。
            if *level != "+" {
                return false;
            }
        }
    }
    true
}

/// 通配符过滤器是否匹配具体主题名（§4.7.2）。
///
/// 调用方应先用 [`is_valid_topic_name`] / [`is_valid_topic_filter`] 校验。
pub fn matches(filter: &str, topic: &str) -> bool {
    // 以 `$` 开头的主题不匹配以通配符（# 或 +）开头的过滤器（§4.7.2）。
    if topic.starts_with('$') && (filter.starts_with('+') || filter.starts_with('#')) {
        return false;
    }

    let mut f = filter.split('/').peekable();
    let mut t = topic.split('/').peekable();

    loop {
        match (f.next(), t.next()) {
            (Some("#"), _) => return true,    // 多级通配：匹配剩余全部
            (Some("+"), Some(_)) => continue, // 单级通配：恰好消费一级
            (Some(flevel), Some(tlevel)) if flevel == tlevel => continue,
            (None, None) => return true,
            _ => return false,
        }
    }
}

fn contains_nul(s: &str) -> bool {
    s.contains('\u{0000}')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn topic_name_validation() {
        assert!(is_valid_topic_name("a/b/c"));
        assert!(is_valid_topic_name("sport/tennis/player1"));
        assert!(!is_valid_topic_name(""));
        assert!(!is_valid_topic_name("a/+/b"));
        assert!(!is_valid_topic_name("a/#"));
    }

    #[test]
    fn filter_validation() {
        assert!(is_valid_topic_filter("a/b"));
        assert!(is_valid_topic_filter("a/+/c"));
        assert!(is_valid_topic_filter("a/#"));
        assert!(is_valid_topic_filter("#"));
        assert!(is_valid_topic_filter("+"));
        assert!(!is_valid_topic_filter(""));
        assert!(!is_valid_topic_filter("a/#/b"));
        assert!(!is_valid_topic_filter("a/b#"));
        assert!(!is_valid_topic_filter("a+b"));
    }

    #[test]
    fn wildcard_matching_examples_from_spec() {
        // §4.7.2 表格中的示例。
        assert!(matches("sport/tennis/player1", "sport/tennis/player1"));
        assert!(matches("sport/tennis/+", "sport/tennis/player1"));
        assert!(matches("sport/tennis/+", "sport/tennis/player2"));
        assert!(!matches("sport/tennis/+", "sport/tennis/player1/ranking"));
        assert!(matches("sport/#", "sport/tennis/player1/ranking"));
        assert!(matches("sport/#", "sport/tennis"));
        assert!(matches("sport/+", "sport/"));
        assert!(!matches("sport/+", "sport"));
        assert!(matches("+/+", "/finance"));
        assert!(!matches("+", "/finance"));
    }

    #[test]
    fn dollar_topics_not_matched_by_wildcards() {
        assert!(!matches("$SYS/broker/uptime", "#"));
        assert!(!matches("$SYS/x", "+/x"));
        // 精确过滤器仍可匹配 $ 主题。
        assert!(matches("$SYS/x", "$SYS/x"));
    }
}
