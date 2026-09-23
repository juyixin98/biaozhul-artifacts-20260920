//! 随构建产物分发的样例 WASM 模块（`wat/*.wat` 由 build.rs 编译到 `wasm/*.wasm`）。

use std::collections::BTreeMap;

/// 样例元信息。
pub struct Sample {
    pub name: &'static str,
    pub wasm: &'static [u8],
    /// 用途说明（README/API 原样返回）。
    pub description: &'static str,
    /// 演示输入含义。
    pub input_hint: &'static str,
}

macro_rules! include_sample {
    ($name:literal) => {
        include_bytes!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/wasm/",
            $name,
            ".wasm"
        ))
    };
}

/// 全部样例（有序）。
pub fn samples() -> Vec<Sample> {
    vec![
        Sample {
            name: "finite_loop",
            wasm: include_sample!("finite_loop"),
            description: "有限循环：1..=n 累加，事务内 kv_put + 真实 SHA-256 + kv_get 读回。",
            input_hint: "8 字节小端 u64，n（如 100000）",
        },
        Sample {
            name: "infinite_loop",
            wasm: include_sample!("infinite_loop"),
            description: "无限循环：先写 poison 再死循环；燃料耗尽终止，poison 不得发布。",
            input_hint: "忽略输入，可用空字节",
        },
        Sample {
            name: "trap_after_write",
            wasm: include_sample!("trap_after_write"),
            description: "陷阱后写入：先写 poison 再 unreachable；事务整体回滚。",
            input_hint: "忽略输入",
        },
        Sample {
            name: "memory_grow",
            wasm: include_sample!("memory_grow"),
            description: "内存增长：循环 memory.grow 直到越过版本内存上限并被终止。",
            input_hint: "8 字节小端 u64，每次增长页数（如 1）",
        },
        Sample {
            name: "oob_read",
            wasm: include_sample!("oob_read"),
            description: "WASM 指令越界：从内存末端边界读取，立即 memory_out_of_bounds。",
            input_hint: "忽略输入",
        },
        Sample {
            name: "host_bad_ptr",
            wasm: include_sample!("host_bad_ptr"),
            description: "宿主函数指针越界：kv_get 输出指针越过线性内存，宿主边界校验终止。",
            input_hint: "忽略输入",
        },
        Sample {
            name: "denied_import",
            wasm: include_sample!("denied_import"),
            description: "非白名单导入：尝试导入 env.socket_connect，实例化阶段 link_error。",
            input_hint: "忽略输入",
        },
    ]
}

pub fn by_name(name: &str) -> Option<Sample> {
    samples().into_iter().find(|s| s.name == name)
}

pub fn map() -> BTreeMap<&'static str, &'static [u8]> {
    samples().into_iter().map(|s| (s.name, s.wasm)).collect()
}
