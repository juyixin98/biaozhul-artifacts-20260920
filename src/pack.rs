//! 打包核心：扫描目录 → 规范化 → 排序 → 生成清单与确定性 tar 字节。
//!
//! 确定性保证：
//! - 文件排序：按规范化路径的字节序（BTreeMap 键序），与 read_dir 枚举顺序无关；
//! - 权限映射：文件有任何执行位 → 0o755，否则 0o644；目录 0o755；符号链接 0o777；
//! - 时间戳：所有条目 mtime 固定为 0，与文件实际 mtime、宿主时区无关；
//! - 冲突：两条原始路径规范化为同一路径 → 返回 Conflict 错误，拒绝打包。

use crate::normalize::{normalize_path, normalize_symlink_target, NormError};
use crate::tar::{EntryType, TarEntryMeta, TarWriter};
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fmt;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

#[derive(Debug)]
pub enum PackError {
    RootNotFound(PathBuf),
    RootNotDir(PathBuf),
    Normalize { path: String, err: NormError },
    Conflict(String),
    UnsupportedType(String),
    Tar(crate::tar::TarError),
    Io(io::Error),
}

impl fmt::Display for PackError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            PackError::RootNotFound(p) => write!(f, "root not found: {}", p.display()),
            PackError::RootNotDir(p) => write!(f, "root is not a directory: {}", p.display()),
            PackError::Normalize { path, err } => write!(f, "invalid path {path:?}: {err}"),
            PackError::Conflict(p) => write!(f, "conflicting normalized path: {p:?}"),
            PackError::UnsupportedType(p) => {
                write!(f, "unsupported file type (not file/dir/symlink): {p:?}")
            }
            PackError::Tar(e) => write!(f, "tar error: {e}"),
            PackError::Io(e) => write!(f, "io error: {e}"),
        }
    }
}

impl std::error::Error for PackError {}

impl From<io::Error> for PackError {
    fn from(e: io::Error) -> Self {
        PackError::Io(e)
    }
}

impl From<crate::tar::TarError> for PackError {
    fn from(e: crate::tar::TarError) -> Self {
        PackError::Tar(e)
    }
}

#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    File,
    Dir,
    Symlink,
}

#[derive(Debug, Clone, Serialize)]
pub struct ManifestEntry {
    pub path: String,
    pub kind: Kind,
    /// 八进制权限字符串，如 "0644"
    pub mode: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub size: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub sha256: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub link_target: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct Manifest {
    pub format: String,
    pub entries: Vec<ManifestEntry>,
    pub archive: ArchiveDigest,
}

#[derive(Debug, Serialize)]
pub struct ArchiveDigest {
    pub sha256: String,
    pub size_bytes: u64,
}

/// 内部条目表示。
struct Entry {
    kind: Kind,
    mode: u32,
    data: Vec<u8>,          // 仅文件
    link_target: String,    // 仅符号链接
    sha256: Option<String>, // 仅文件
}

/// 权限映射规则：执行位 → 755，否则 644；目录 755；符号链接 777。
fn map_mode(meta: &fs::Metadata, kind: &Kind) -> u32 {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let raw = meta.permissions().mode();
        match kind {
            Kind::Dir => 0o755,
            Kind::Symlink => 0o777,
            Kind::File => {
                if raw & 0o111 != 0 {
                    0o755
                } else {
                    0o644
                }
            }
        }
    }
    #[cfg(not(unix))]
    {
        let _ = meta;
        match kind {
            Kind::Dir => 0o755,
            Kind::Symlink => 0o777,
            Kind::File => 0o644,
        }
    }
}

fn sha256_hex(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(data);
    hex_encode(&h.finalize())
}

pub fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(bytes.len() * 2);
    for &b in bytes {
        s.push(HEX[(b >> 4) as usize] as char);
        s.push(HEX[(b & 0xf) as usize] as char);
    }
    s
}

/// 向条目表插入一条，规范化路径冲突时返回错误。
fn insert(
    entries: &mut BTreeMap<String, Entry>,
    raw_path: &str,
    entry: Entry,
) -> Result<(), PackError> {
    let norm = normalize_path(raw_path).map_err(|err| PackError::Normalize {
        path: raw_path.to_string(),
        err,
    })?;
    if entries.contains_key(&norm) {
        return Err(PackError::Conflict(norm));
    }
    entries.insert(norm, entry);
    Ok(())
}

/// 确保路径的所有父目录都有目录条目（幂等，不覆盖已有条目）。
fn ensure_parents(entries: &mut BTreeMap<String, Entry>, norm_path: &str) {
    let parts: Vec<&str> = norm_path.split('/').collect();
    for i in 1..parts.len() {
        let dir = parts[..i].join("/");
        entries.entry(dir).or_insert(Entry {
            kind: Kind::Dir,
            mode: 0o755,
            data: Vec::new(),
            link_target: String::new(),
            sha256: None,
        });
    }
}

/// 递归扫描目录，收集条目（不跟随符号链接）。
fn scan_dir(root: &Path, rel: &str, entries: &mut BTreeMap<String, Entry>) -> Result<(), PackError> {
    // read_dir 顺序未定义 —— 但所有条目最终进入 BTreeMap 按键排序，与枚举顺序无关。
    let dir = if rel.is_empty() { root.to_path_buf() } else { root.join(rel) };
    for item in fs::read_dir(&dir)? {
        let item = item?;
        let name = item.file_name();
        let name = name.to_str().ok_or(PackError::Normalize {
            path: format!("{rel}/{name:?}"),
            err: NormError::NonUtf8,
        })?;
        let child_rel = if rel.is_empty() { name.to_string() } else { format!("{rel}/{name}") };
        let meta = fs::symlink_metadata(item.path())?;
        let ft = meta.file_type();
        if ft.is_symlink() {
            let raw_target = fs::read_link(item.path())?;
            let raw_target = raw_target.to_str().ok_or(PackError::Normalize {
                path: child_rel.clone(),
                err: NormError::NonUtf8,
            })?;
            let norm = normalize_path(&child_rel).map_err(|err| PackError::Normalize {
                path: child_rel.clone(),
                err,
            })?;
            let parts: Vec<&str> = norm.split('/').collect();
            let parent = &parts[..parts.len() - 1];
            let target = normalize_symlink_target(parent, raw_target).map_err(|err| {
                PackError::Normalize {
                    path: format!("{child_rel} -> {raw_target}"),
                    err,
                }
            })?;
            insert(
                entries,
                &child_rel,
                Entry {
                    kind: Kind::Symlink,
                    mode: 0o777,
                    data: Vec::new(),
                    link_target: target,
                    sha256: None,
                },
            )?;
        } else if ft.is_dir() {
            insert(
                entries,
                &child_rel,
                Entry {
                    kind: Kind::Dir,
                    mode: 0o755,
                    data: Vec::new(),
                    link_target: String::new(),
                    sha256: None,
                },
            )?;
            scan_dir(root, &child_rel, entries)?;
        } else if ft.is_file() {
            let data = fs::read(item.path())?;
            let digest = sha256_hex(&data);
            let mode = map_mode(&meta, &Kind::File);
            insert(
                entries,
                &child_rel,
                Entry {
                    kind: Kind::File,
                    mode,
                    data,
                    link_target: String::new(),
                    sha256: Some(digest),
                },
            )?;
        } else {
            return Err(PackError::UnsupportedType(child_rel));
        }
    }
    Ok(())
}

/// 打包结果：清单 + tar 字节。
#[derive(Debug)]
pub struct PackResult {
    pub manifest: Manifest,
    pub tar_bytes: Vec<u8>,
}

/// 打包一个目录。
///
/// - `paths` 为 None 时扫描整个目录；
/// - 为 Some 时仅打包列出的相对路径（用于显式文件列表，冲突检测同样生效）。
pub fn pack(root: &Path, paths: Option<&[String]>) -> Result<PackResult, PackError> {
    if !root.exists() {
        return Err(PackError::RootNotFound(root.to_path_buf()));
    }
    if !root.is_dir() {
        return Err(PackError::RootNotDir(root.to_path_buf()));
    }

    let mut entries: BTreeMap<String, Entry> = BTreeMap::new();

    match paths {
        None => scan_dir(root, "", &mut entries)?,
        Some(list) => {
            for raw in list {
                let norm = normalize_path(raw).map_err(|err| PackError::Normalize {
                    path: raw.clone(),
                    err,
                })?;
                let fs_path = root.join(&norm);
                let meta = fs::symlink_metadata(&fs_path).map_err(|e| {
                    if e.kind() == io::ErrorKind::NotFound {
                        PackError::RootNotFound(fs_path.clone())
                    } else {
                        PackError::Io(e)
                    }
                })?;
                let ft = meta.file_type();
                if ft.is_symlink() {
                    let raw_target = fs::read_link(&fs_path)?;
                    let raw_target = raw_target.to_str().ok_or(PackError::Normalize {
                        path: norm.clone(),
                        err: NormError::NonUtf8,
                    })?;
                    let parts: Vec<&str> = norm.split('/').collect();
                    let parent = &parts[..parts.len() - 1];
                    let target =
                        normalize_symlink_target(parent, raw_target).map_err(|err| {
                            PackError::Normalize {
                                path: format!("{norm} -> {raw_target}"),
                                err,
                            }
                        })?;
                    insert(
                        &mut entries,
                        raw,
                        Entry {
                            kind: Kind::Symlink,
                            mode: 0o777,
                            data: Vec::new(),
                            link_target: target,
                            sha256: None,
                        },
                    )?;
                } else if ft.is_dir() {
                    scan_dir(root, &norm, &mut entries)?;
                    // 目录本身也要有条目
                    entries.entry(norm.clone()).or_insert(Entry {
                        kind: Kind::Dir,
                        mode: 0o755,
                        data: Vec::new(),
                        link_target: String::new(),
                        sha256: None,
                    });
                } else if ft.is_file() {
                    let data = fs::read(&fs_path)?;
                    let digest = sha256_hex(&data);
                    let mode = map_mode(&meta, &Kind::File);
                    insert(
                        &mut entries,
                        raw,
                        Entry {
                            kind: Kind::File,
                            mode,
                            data,
                            link_target: String::new(),
                            sha256: Some(digest),
                        },
                    )?;
                } else {
                    return Err(PackError::UnsupportedType(norm));
                }
                ensure_parents(&mut entries, &norm);
            }
        }
    }

    // 生成 tar 字节与清单（BTreeMap 迭代即字节序排序）。
    let mut tw = TarWriter::new(Vec::new());
    let mut manifest_entries = Vec::with_capacity(entries.len());
    for (path, e) in &entries {
        let entry_type = match e.kind {
            Kind::File => EntryType::File,
            Kind::Dir => EntryType::Dir,
            Kind::Symlink => EntryType::Symlink,
        };
        // 目录条目在 tar 中以 '/' 结尾
        let tar_path = if e.kind == Kind::Dir {
            format!("{path}/")
        } else {
            path.clone()
        };
        let meta = TarEntryMeta {
            path: &tar_path,
            entry_type,
            mode: e.mode,
            size: e.data.len() as u64,
            link_target: if e.kind == Kind::Symlink {
                Some(&e.link_target)
            } else {
                None
            },
        };
        tw.append(&meta, &e.data)?;
        manifest_entries.push(ManifestEntry {
            path: path.clone(),
            kind: e.kind.clone(),
            mode: format!("{:04o}", e.mode),
            size: if e.kind == Kind::File { Some(e.data.len() as u64) } else { None },
            sha256: e.sha256.clone(),
            link_target: if e.kind == Kind::Symlink {
                Some(e.link_target.clone())
            } else {
                None
            },
        });
    }
    let tar_bytes = tw.finish()?;
    let digest = sha256_hex(&tar_bytes);

    Ok(PackResult {
        manifest: Manifest {
            format: "repro-pack/1".to_string(),
            entries: manifest_entries,
            archive: ArchiveDigest {
                sha256: digest,
                size_bytes: tar_bytes.len() as u64,
            },
        },
        tar_bytes,
    })
}
