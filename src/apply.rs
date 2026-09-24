//! 计划落盘：在暂存目录中构建完整输出树，再原子换名为目标目录。
//!
//! # 原子性保证（Unix）
//!
//! 1. 在目标目录的**同一父目录**下创建两个临时目录：
//!    - `stage-XXXX`：完整的新输出树；
//!    - `obj-XXXX`：内容寻址的去重对象池，相同内容只写一份，
//!      再通过硬链接（失败时回退复制）进入输出树，构建完成后删除。
//! 2. 只有计划零冲突且暂存树**完整构建成功**后才触碰目标路径：
//!    - 目标不存在：`rename(stage -> target)`，单次原子改名；
//!    - 目标已存在（目录）：先 `rename(target -> backup)`，再
//!      `rename(stage -> target)`；第二步失败则把 backup 改回去。
//! 3. 任何失败：删除暂存目录、尽力恢复原状，目标树保持调用前状态。
//!    备份目录保留在原位置，返回错误。
//!
//! 注：目标存在时的「备份→换入」是两次 rename，两次之间若进程被
//! SIGKILL/断电，窗口内目标路径缺失。单目录原子交换需要 `renameat2`
//! 的 `RENAME_EXCHANGE`（Linux 3.15+），如需更强保证可后续扩展，README
//! 已如实记录该限制。
//!
//! 仅在 Unix 上编译（依赖 `std::os::unix::fs::symlink`）。

use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use base64::Engine;

use crate::model::*;
use crate::plan;

/// 落盘结果。计划有冲突时返回 [`ApplyError::Conflict`]，且不触碰文件系统。
pub fn apply(request: &MergeRequest) -> Result<ApplyResult, ApplyError> {
    let plan = plan::build_plan(request).map_err(|e| match e {
        plan::PlanError::BadRequest(m) => ApplyError::BadRequest(m),
    })?;
    if !plan.ok() {
        return Err(ApplyError::Conflict(Box::new(plan)));
    }

    let target = PathBuf::from(&request.target_dir);
    if target.as_os_str().is_empty() {
        return Err(ApplyError::Io(io::Error::new(
            io::ErrorKind::InvalidInput,
            "target_dir must not be empty",
        )));
    }
    let parent = target
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."));
    let target_name = target
        .file_name()
        .ok_or_else(|| {
            ApplyError::Io(io::Error::new(
                io::ErrorKind::InvalidInput,
                "target_dir must have a final component",
            ))
        })?
        .to_owned();

    fs::create_dir_all(&parent).map_err(|e| {
        ApplyError::Io(io::Error::new(
            e.kind(),
            format!("cannot create parent directory {}: {e}", parent.display()),
        ))
    })?;

    // 目标现状必须不存在或为目录（不覆盖文件 / 符号链接）。
    let target_existed = match target.symlink_metadata() {
        Ok(md) => {
            if md.is_dir() {
                true
            } else {
                return Err(ApplyError::Io(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    format!("{} exists and is not a directory", target.display()),
                )));
            }
        }
        Err(e) if e.kind() == io::ErrorKind::NotFound => false,
        Err(e) => {
            return Err(ApplyError::Io(io::Error::new(
                e.kind(),
                format!("cannot stat {}: {e}", target.display()),
            )));
        }
    };

    // 唯一临时前缀（PID + 纳秒 + 计数器无需，用时间即可）。
    let stamp = format!(
        "{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    );
    let stage = parent.join(format!(
        ".{}.build-merge.stage-{stamp}",
        target_name.to_string_lossy()
    ));
    let objdir = parent.join(format!(
        ".{}.build-merge.obj-{stamp}",
        target_name.to_string_lossy()
    ));
    let backup = parent.join(format!(
        ".{}.build-merge.backup-{stamp}",
        target_name.to_string_lossy()
    ));

    use sha2::Digest;

    fs::create_dir(&stage)
        .map_err(|e| io_err(e, "create stage dir", &stage))
        .map_err(ApplyError::Io)?;
    if let Err(e) = fs::create_dir(&objdir) {
        let _ = fs::remove_dir_all(&stage);
        return Err(ApplyError::Io(io_err(e, "create object dir", &objdir)));
    }

    let outcome = (|| -> io::Result<ApplyStats> {
        let mut stats = ApplyStats::default();

        // 1) 目录（计划已按路径排序，父目录总在子目录之前）。
        for dir in plan.dirs.keys() {
            let p = join_relative(&stage, dir)?;
            fs::create_dir_all(&p)?;
            stats.dirs += 1;
        }

        // 2) 对象池：相同 sha256 只解码、写入一次。
        //    先收集每个 hash 的来源内容（从请求中任取一份即可，
        //    同 hash 即同内容）。
        let mut hash_to_bytes: HashMap<String, Vec<u8>> = HashMap::new();
        for action in &request.actions {
            for entry in &action.outputs {
                if entry.kind == EntryKind::File {
                    let raw = entry.content_base64.as_deref().unwrap_or("");
                    let bytes = base64::engine::general_purpose::STANDARD
                        .decode(raw)
                        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
                    let mut hasher = sha2::Sha256::new();
                    sha2::Digest::update(&mut hasher, &bytes);
                    let hash = crate::plan::plan_hex(hasher.finalize());
                    hash_to_bytes.entry(hash).or_insert(bytes);
                }
            }
        }
        for hash in hash_to_bytes.keys() {
            let obj = hash_to_path(&objdir, hash);
            fs::create_dir_all(obj.parent().unwrap())?;
            fs::write(&obj, &hash_to_bytes[hash])?;
            stats.objects += 1;
        }

        // 3) 文件：优先硬链接对象，跨设备等失败时回退复制。
        for (path, pf) in &plan.files {
            let dest = join_relative(&stage, path)?;
            if let Some(parent) = dest.parent() {
                fs::create_dir_all(parent)?;
            }
            let obj = hash_to_path(&objdir, &pf.sha256);
            match fs::hard_link(&obj, &dest) {
                Ok(()) => stats.hardlinked += 1,
                Err(_) => {
                    fs::copy(&obj, &dest)?;
                    stats.copied += 1;
                }
            }
            stats.files += 1;
        }

        // 4) 符号链接（相对目标，直接创建）。
        for (path, sl) in &plan.symlinks {
            let link = join_relative(&stage, path)?;
            if let Some(parent) = link.parent() {
                fs::create_dir_all(parent)?;
            }
            #[cfg(unix)]
            {
                std::os::unix::fs::symlink(&sl.target, &link)?;
            }
            #[cfg(not(unix))]
            {
                let _ = link;
                return Err(io::Error::new(
                    io::ErrorKind::Unsupported,
                    "symlink output is only supported on Unix",
                ));
            }
            stats.symlinks += 1;
        }

        Ok(stats)
    })();

    let stats = match outcome {
        Ok(s) => s,
        Err(e) => {
            // 构建失败：清理暂存与对象池，目标从未被触碰。
            let _ = fs::remove_dir_all(&stage);
            let _ = fs::remove_dir_all(&objdir);
            return Err(ApplyError::Io(e));
        }
    };

    // 5) 原子换名。
    let swap = (|| -> io::Result<()> {
        if target_existed {
            fs::rename(&target, &backup)?;
            if let Err(e) = fs::rename(&stage, &target) {
                // 回退：把备份改回原名。
                let _ = fs::rename(&backup, &target);
                return Err(e);
            }
        } else {
            fs::rename(&stage, &target)?;
        }
        Ok(())
    })();

    if let Err(e) = swap {
        let _ = fs::remove_dir_all(&stage);
        let _ = fs::remove_dir_all(&objdir);
        return Err(ApplyError::Io(io_err(
            e,
            "atomic rename into place",
            &target,
        )));
    }

    // 换名成功后清理备份与对象池（失败仅警告，不影响结果）。
    if target_existed {
        let _ = fs::remove_dir_all(&backup);
    }
    let _ = fs::remove_dir_all(&objdir);

    Ok(ApplyResult {
        target_dir: request.target_dir.clone(),
        files_written: stats.files,
        dirs_created: stats.dirs,
        symlinks_created: stats.symlinks,
        content_objects: stats.objects,
        plan,
    })
}

#[derive(Default, Debug)]
struct ApplyStats {
    files: usize,
    dirs: usize,
    symlinks: usize,
    objects: usize,
    hardlinked: usize,
    copied: usize,
}

/// apply 的错误类型。
#[derive(Debug)]
pub enum ApplyError {
    /// 请求不合法（HTTP 400）。
    BadRequest(String),
    /// 计划存在冲突（HTTP 409），目标树未发生任何变化。
    ///
    /// 装箱是因为完整 [`Plan`] 体积较大，避免让整个错误枚举在栈上膨胀。
    Conflict(Box<Plan>),
    /// 文件系统 IO 错误（HTTP 500）。
    Io(io::Error),
}

fn join_relative(base: &Path, rel: &str) -> io::Result<PathBuf> {
    // rel 已在规划阶段通过严格校验（无 `/` 前缀、无 `..`、无空段），
    // 这里逐段拼接并再次防御性检查。
    let mut p = base.to_path_buf();
    for seg in rel.split('/') {
        if seg.is_empty() || seg == ".." || seg == "." || seg.contains('\0') {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("unsafe relative path after planning: {rel:?}"),
            ));
        }
        p.push(seg);
    }
    Ok(p)
}

fn hash_to_path(objdir: &Path, hash: &str) -> PathBuf {
    let (prefix, rest) = hash.split_at(2);
    objdir.join(prefix).join(rest)
}

fn io_err(e: io::Error, what: &str, p: &Path) -> io::Error {
    io::Error::new(e.kind(), format!("{what} {}: {e}", p.display()))
}
