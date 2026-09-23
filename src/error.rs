//! 错误类型与陷阱分类。
//!
//! 分类结果直接暴露在 API 响应里，保证「失败如实报告」且可解释；
//! 内部细节通过 [`Error`] 的 thiserror 上下文保留在服务端日志。

use thiserror::Error;

/// 终止类别的机器可读代码（出现在 `ExecResponse.status` 中）。
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TerminationKind {
    /// 燃料耗尽（确定性终止）。
    OutOfFuel,
    /// 线性内存越界（WASM 指令或宿主指针校验触发）。
    MemoryOutOfBounds,
    /// 内存/表增长超过计量版本上限。
    MemoryLimitExceeded,
    /// 其它 WASM 陷阱（unreachable、整数除零、不可达等）。
    Trap,
    /// 实例化/链接失败，最典型原因是导入不在白名单。
    LinkError,
    /// 模块字节本身不合法或不受支持。
    CompileError,
    /// 模块不符合宿主 ABI（缺导出等）。
    ModuleRejected,
    /// 请求参数不合法。
    BadRequest,
    /// 宿主内部错误（已如实报告，不把失败伪装成成功）。
    HostError,
}

impl TerminationKind {
    pub fn as_str(self) -> &'static str {
        match self {
            TerminationKind::OutOfFuel => "out_of_fuel",
            TerminationKind::MemoryOutOfBounds => "memory_out_of_bounds",
            TerminationKind::MemoryLimitExceeded => "memory_limit_exceeded",
            TerminationKind::Trap => "trap",
            TerminationKind::LinkError => "link_error",
            TerminationKind::CompileError => "compile_error",
            TerminationKind::ModuleRejected => "module_rejected",
            TerminationKind::BadRequest => "bad_request",
            TerminationKind::HostError => "host_error",
        }
    }
}

impl std::fmt::Display for TerminationKind {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// 结构化执行错误：携带分类与可读消息。
#[derive(Debug, Error)]
#[error("{kind}: {message}")]
pub struct Error {
    pub kind: TerminationKind,
    pub message: String,
}

impl Error {
    pub fn new(kind: TerminationKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }

    pub fn bad_request(msg: impl Into<String>) -> Self {
        Self::new(TerminationKind::BadRequest, msg)
    }

    /// 根据 wasmtime 陷阱做分类。
    pub fn from_trap(trap: &wasmtime::Trap) -> Self {
        let kind = match trap {
            wasmtime::Trap::OutOfFuel => TerminationKind::OutOfFuel,
            wasmtime::Trap::MemoryOutOfBounds => TerminationKind::MemoryOutOfBounds,
            _ => TerminationKind::Trap,
        };
        Error {
            kind,
            message: trap.to_string(),
        }
    }

    /// 根据任意 std 错误做分类（陷阱会穿透若干层上下文）。
    pub fn from_std<E: std::error::Error + Send + Sync + 'static>(err: E) -> Self {
        let ae: anyhow::Error = err.into();
        Self::from_anyhow(&ae)
    }

    /// 根据任意 anyhow 错误链做分类（陷阱会穿透若干层上下文）。
    pub fn from_anyhow(err: &anyhow::Error) -> Self {
        // 1) 链上有明确 Trap
        for cause in err.chain() {
            if let Some(trap) = cause.downcast_ref::<wasmtime::Trap>() {
                return Self::from_trap(trap);
            }
        }
        // 2) 链上已经是我们自己的 Error
        if let Some(e) = err.downcast_ref::<Error>() {
            return Error {
                kind: e.kind,
                message: e.message.clone(),
            };
        }
        // 3) 字符串启发式：链接失败（未知导入）/编译失败/内存上限。
        let text = format!("{err:#}");
        let kind = if text.contains("unknown import") || text.contains("incompatible import type") {
            TerminationKind::LinkError
        } else if text.contains("memory limit exceeded") || text.contains("table limit exceeded") {
            TerminationKind::MemoryLimitExceeded
        } else if text.contains("failed to compile WebAssembly module")
            || text.contains("expected WebAssembly module")
        {
            TerminationKind::CompileError
        } else {
            TerminationKind::HostError
        };
        Error {
            kind,
            message: text,
        }
    }
}

pub type Result<T> = std::result::Result<T, Error>;
