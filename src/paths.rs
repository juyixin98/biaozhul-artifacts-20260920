//! 路径与符号链接目标的校验、规范化、大小写折叠。
//!
//! 所有输出路径都按 POSIX 风格（`/` 分隔）的相对路径处理，与运行平台解耦：
//! 这样在任意平台上规划结果都一致；落盘（`apply`）只在 Unix 上支持。

use caseless::default_case_fold_str;

/// 将一条输出路径切分为经过校验的路径段。
///
/// 拒绝：空路径、绝对路径（前导 `/`）、空段（`a//b`、尾随 `/`）、
/// `.` / `..` 段、包含 NUL 字节的段、Windows 盘符或反斜杠。
pub fn split_validated(path: &str) -> Result<Vec<String>, String> {
    if path.is_empty() {
        return Err("path is empty".into());
    }
    if path.contains('\0') {
        return Err(format!("path contains NUL byte: {path:?}"));
    }
    if path.contains('\\') {
        return Err(format!("backslashes are not allowed: {path:?}"));
    }
    if path.starts_with('/') {
        return Err(format!("path must be relative: {path:?}"));
    }
    // Windows 盘符（如 `C:...`）——反斜杠已拒绝，这里再挡前导盘符。
    if let Some((drive, rest)) = path.split_once(':')
        && drive.len() == 1
        && drive
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_alphabetic())
        && (rest.is_empty() || rest.starts_with('/'))
    {
        return Err(format!("drive letters are not allowed: {path:?}"));
    }
    let mut segs = Vec::new();
    for seg in path.split('/') {
        match seg {
            "" => return Err(format!("empty path segment in {path:?}")),
            "." | ".." => return Err(format!("`.`/`..` segments are not allowed: {path:?}")),
            s => segs.push(s.to_string()),
        }
    }
    Ok(segs)
}

/// 对单个路径段做 Unicode 默认大小写折叠（caseless，不含尾 sigma 特殊规则）。
pub fn fold_segment(seg: &str) -> String {
    default_case_fold_str(seg)
}

/// 词法规范化一个相对路径：解析 `.` 与可在内部消解的 `..`，
/// 返回 `(规范化段, 无法消解的前导 .. 数量)`。
///
/// 例如 `a/../b` -> `(["b"], 0)`；`../a/./b` -> `(["a","b"], 1)`。
fn lexically_normalize(segs: &[&str]) -> (Vec<String>, usize) {
    let mut leading_parent = 0usize;
    let mut out: Vec<String> = Vec::new();
    for seg in segs {
        match *seg {
            "." => {}
            ".." => {
                if out.pop().is_none() {
                    leading_parent += 1;
                }
            }
            s => out.push(s.to_string()),
        }
    }
    (out, leading_parent)
}

/// 符号链接目标规范化与逃逸校验。
///
/// `link_segs` 是链接自身在输出树中的路径段；相对目标相对链接所在目录解析。
/// 规则：
/// - 目标必须是非空相对路径（拒绝绝对路径）；
/// - 不允许 NUL、反斜杠、空段；
/// - 解析 `.`/`..` 后，从链接所在目录向上回退的层数不得超出该目录深度
///   （即规范化后的绝对解析路径不得逃逸出输出根）；
/// - 成功时返回规范化后的目标字符串（前导 `..` 会保留，因为它们语义合法）。
pub fn normalize_symlink_target(link_segs: &[String], target: &str) -> Result<String, String> {
    if target.is_empty() {
        return Err("symlink target is empty".into());
    }
    if target.contains('\0') {
        return Err(format!("symlink target contains NUL byte: {target:?}"));
    }
    if target.contains('\\') {
        return Err(format!(
            "backslashes are not allowed in symlink target: {target:?}"
        ));
    }
    if target.starts_with('/') {
        return Err(format!(
            "symlink target must be relative, got absolute: {target:?}"
        ));
    }
    let raw: Vec<&str> = target.split('/').collect();
    for seg in &raw {
        if seg.is_empty() {
            return Err(format!("empty segment in symlink target: {target:?}"));
        }
    }
    let (normalized, leading_parent) = lexically_normalize(&raw);

    // 链接所在目录深度 = 链接路径段数 - 1。
    let dir_depth = link_segs.len().saturating_sub(1);
    if leading_parent > dir_depth {
        return Err(format!(
            "symlink target escapes output root: link {:?} -> {target:?}",
            link_segs.join("/")
        ));
    }

    let mut parts: Vec<String> = Vec::new();
    for _ in 0..leading_parent {
        parts.push("..".into());
    }
    parts.extend(normalized);
    Ok(parts.join("/"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_output_paths() {
        assert!(split_validated("a/b/c").is_ok());
        assert!(split_validated("").is_err());
        assert!(split_validated("/a").is_err());
        assert!(split_validated("a//b").is_err());
        assert!(split_validated("a/").is_err());
        assert!(split_validated("./a").is_err());
        assert!(split_validated("a/../b").is_err());
        assert!(split_validated("a\\b").is_err());
        assert!(split_validated("C:/x").is_err());
    }

    #[test]
    fn folds_case_unicode() {
        assert_eq!(fold_segment("FILE.TXT"), fold_segment("file.txt"));
        // U+004B LATIN CAPITAL LETTER K 与 U+212A KELVIN SIGN 默认折叠等价。
        assert_eq!(fold_segment("\u{212A}"), fold_segment("K"));
    }

    #[test]
    fn normalizes_symlink_targets() {
        let link = |p: &str| split_validated(p).unwrap();

        assert_eq!(
            normalize_symlink_target(&link("a/link"), "./b").unwrap(),
            "b"
        );
        assert_eq!(
            normalize_symlink_target(&link("a/link"), "x/../b").unwrap(),
            "b"
        );
        // 回退一层：链接在 a/ 下，`../b` 解析到根下 b，合法。
        assert_eq!(
            normalize_symlink_target(&link("a/link"), "../b").unwrap(),
            "../b"
        );
        // 回退两层超出目录深度 1，逃逸。
        assert!(normalize_symlink_target(&link("a/link"), "../../etc/passwd").is_err());
        // 根目录下的链接不允许任何前导 ..。
        assert!(normalize_symlink_target(&link("link"), "../x").is_err());
        assert!(normalize_symlink_target(&link("a/link"), "/abs").is_err());
        assert!(normalize_symlink_target(&link("a/link"), "").is_err());
        // 深层链接可回退多层。
        assert_eq!(
            normalize_symlink_target(&link("a/b/link"), "../../x").unwrap(),
            "../../x"
        );
        assert!(normalize_symlink_target(&link("a/b/link"), "../../../x").is_err());
    }
}
