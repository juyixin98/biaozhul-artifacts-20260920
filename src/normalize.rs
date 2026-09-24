//! 规范化路径规则（显式规范，见 README）：
//!
//! 1. 路径一律使用 `/` 分隔；输入中的 `\` 不视为分隔符（按普通字符处理，
//!    但为避免跨平台歧义，直接拒绝含 `\` 的路径）。
//! 2. 拒绝：空路径、绝对路径（以 `/` 开头）、NUL 字符、任何 `..` 组件。
//! 3. 忽略空组件（`a//b` → `a/b`）与 `.` 组件（`a/./b` → `a/b`）。
//! 4. 规范化后的路径按字节序（UTF-8 码点序）排序，保证与目录枚举顺序无关。
//! 5. 同一包内两条不同的原始路径若规范化为同一路径，视为冲突，必须拒绝。

use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NormError {
    Empty,
    Absolute,
    ParentDir,
    Backslash,
    Nul,
    NonUtf8,
    EmptyAfterNormalize,
}

impl fmt::Display for NormError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let msg = match self {
            NormError::Empty => "path is empty",
            NormError::Absolute => "absolute paths are not allowed",
            NormError::ParentDir => "'..' components are not allowed",
            NormError::Backslash => "backslash is not allowed in paths",
            NormError::Nul => "NUL byte is not allowed in paths",
            NormError::NonUtf8 => "path is not valid UTF-8",
            NormError::EmptyAfterNormalize => "path normalizes to empty",
        };
        f.write_str(msg)
    }
}

/// 将一条相对路径规范化为确定形式（`/` 分隔、无 `.`/空组件、无首尾斜杠）。
pub fn normalize_path(raw: &str) -> Result<String, NormError> {
    if raw.is_empty() {
        return Err(NormError::Empty);
    }
    if raw.starts_with('/') {
        return Err(NormError::Absolute);
    }
    if raw.contains('\\') {
        return Err(NormError::Backslash);
    }
    if raw.contains('\0') {
        return Err(NormError::Nul);
    }
    let mut parts: Vec<&str> = Vec::new();
    for comp in raw.split('/') {
        match comp {
            "" | "." => continue,
            ".." => return Err(NormError::ParentDir),
            c => parts.push(c),
        }
    }
    if parts.is_empty() {
        return Err(NormError::EmptyAfterNormalize);
    }
    Ok(parts.join("/"))
}

/// 校验符号链接目标并给出规范化目标。
///
/// 符号链接规则（显式规范）：
/// - 目标必须是相对路径（拒绝绝对目标）；
/// - 目标中允许 `..`，但相对链接所在目录解析后不得逃逸出根目录；
/// - 目标会被规范化（解析 `.` 与 `..`），保证同一逻辑目标字节一致。
pub fn normalize_symlink_target(link_dir_components: &[&str], raw_target: &str) -> Result<String, NormError> {
    if raw_target.is_empty() {
        return Err(NormError::Empty);
    }
    if raw_target.starts_with('/') {
        return Err(NormError::Absolute);
    }
    if raw_target.contains('\\') {
        return Err(NormError::Backslash);
    }
    if raw_target.contains('\0') {
        return Err(NormError::Nul);
    }
    // 从链接所在目录出发解析目标，深度不得低于 0（根）。
    let mut depth: Vec<String> = link_dir_components.iter().map(|s| s.to_string()).collect();
    for comp in raw_target.split('/') {
        match comp {
            "" | "." => continue,
            ".." => {
                if depth.pop().is_none() {
                    return Err(NormError::ParentDir); // 逃逸出根
                }
            }
            c => depth.push(c.to_string()),
        }
    }
    if depth.is_empty() {
        return Err(NormError::EmptyAfterNormalize);
    }
    Ok(depth.join("/"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_basic() {
        assert_eq!(normalize_path("a/b/c.txt").unwrap(), "a/b/c.txt");
        assert_eq!(normalize_path("a//b/./c.txt").unwrap(), "a/b/c.txt");
        assert_eq!(normalize_path("./a.txt").unwrap(), "a.txt");
        assert_eq!(normalize_path("a/b/").unwrap(), "a/b");
    }

    #[test]
    fn rejects_bad_paths() {
        assert_eq!(normalize_path(""), Err(NormError::Empty));
        assert_eq!(normalize_path("/etc/passwd"), Err(NormError::Absolute));
        assert_eq!(normalize_path("../x"), Err(NormError::ParentDir));
        assert_eq!(normalize_path("a/../../x"), Err(NormError::ParentDir));
        assert_eq!(normalize_path("a\\b"), Err(NormError::Backslash));
        assert_eq!(normalize_path("."), Err(NormError::EmptyAfterNormalize));
        assert_eq!(normalize_path("./"), Err(NormError::EmptyAfterNormalize));
    }

    #[test]
    fn symlink_targets() {
        // 链接 dir/ln -> ../sibling.txt 解析为 sibling.txt（根内，允许）
        assert_eq!(
            normalize_symlink_target(&["dir"], "../sibling.txt").unwrap(),
            "sibling.txt"
        );
        // 逃逸根目录：拒绝
        assert_eq!(
            normalize_symlink_target(&["dir"], "../../outside.txt"),
            Err(NormError::ParentDir)
        );
        assert_eq!(
            normalize_symlink_target(&[], "../outside.txt"),
            Err(NormError::ParentDir)
        );
        // 绝对目标：拒绝
        assert_eq!(
            normalize_symlink_target(&["dir"], "/etc/passwd"),
            Err(NormError::Absolute)
        );
        // 根内普通相对目标
        assert_eq!(
            normalize_symlink_target(&["a", "b"], "./c/d.txt").unwrap(),
            "a/b/c/d.txt"
        );
    }
}
