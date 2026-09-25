//! 主题名 / 主题过滤器校验与匹配（MQTT 3.1.1 §4.7）。

/// 发布用主题名：非空、不含通配符、不含 NUL。
pub fn valid_topic_name(topic: &str) -> bool {
    !topic.is_empty()
        && !topic.contains('+')
        && !topic.contains('#')
        && !topic.contains('\u{0000}')
}

/// 订阅过滤器：`+` 必须独占一层，`#` 必须是最后一层且独占。
pub fn valid_filter(filter: &str) -> bool {
    if filter.is_empty() || filter.contains('\u{0000}') {
        return false;
    }
    let levels: Vec<&str> = filter.split('/').collect();
    for (i, lv) in levels.iter().enumerate() {
        if lv.contains('#') && !(*lv == "#" && i == levels.len() - 1) {
            return false;
        }
        if lv.contains('+') && *lv != "+" {
            return false;
        }
    }
    true
}

/// 过滤器是否匹配主题名。假定两者均已通过校验。
/// 以通配符开头的过滤器不匹配 `$` 开头的系统主题（§4.7.2）。
pub fn matches(filter: &str, topic: &str) -> bool {
    if topic.starts_with('$') && (filter.starts_with('+') || filter.starts_with('#')) {
        return false;
    }
    let mut f = filter.split('/');
    let mut t = topic.split('/');
    loop {
        match (f.next(), t.next()) {
            (Some("#"), _) => return true,
            (Some("+"), Some(_)) => continue,
            (Some(a), Some(b)) if a == b => continue,
            (None, None) => return true,
            _ => return false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn filter_validation() {
        assert!(valid_filter("a/b/c"));
        assert!(valid_filter("a/+/c"));
        assert!(valid_filter("a/#"));
        assert!(valid_filter("#"));
        assert!(valid_filter("+/+"));
        assert!(!valid_filter(""));
        assert!(!valid_filter("a/#/b"));
        assert!(!valid_filter("a/b#"));
        assert!(!valid_filter("a/b+"));
        assert!(!valid_filter("a/\u{0000}"));
    }

    #[test]
    fn topic_name_validation() {
        assert!(valid_topic_name("a/b"));
        assert!(!valid_topic_name(""));
        assert!(!valid_topic_name("a/+/b"));
        assert!(!valid_topic_name("a/#"));
    }

    #[test]
    fn matching() {
        assert!(matches("a/b/c", "a/b/c"));
        assert!(matches("a/+/c", "a/b/c"));
        assert!(matches("a/#", "a/b/c/d"));
        assert!(matches("#", "a/b"));
        assert!(matches("a/+", "a/b"));
        assert!(!matches("a/+", "a/b/c"));
        assert!(!matches("a/b", "a/b/c"));
        assert!(!matches("a/b/c", "a/b"));
        assert!(!matches("a/+/c", "a/b/d"));
        // 通配符不匹配以 $ 开头的系统主题（MQTT 3.1.1 §4.7.2）
        assert!(!matches("#", "$SYS/broker"));
        assert!(!matches("+/x", "$SYS/x"));
    }
}
