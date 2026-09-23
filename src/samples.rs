//! 内置示例模块。
//!
//! WAT 样例在运行时由 `wat` crate 编译；预编译的 Rust(no_std) SHA-256
//! guest 以 wasm 字节形式直接嵌入（源码见 wasm_modules/sha256guest）。

pub enum SampleSource {
    Wat(&'static str),
    Wasm(&'static [u8]),
}

pub struct Sample {
    pub id: &'static str,
    pub description: &'static str,
    pub source: SampleSource,
}

pub const SAMPLES: &[Sample] = &[
    Sample {
        id: "finite_loop",
        description: "有限循环：对输入字节做真实整数累加，写 KV 与输出，正常提交。",
        source: SampleSource::Wat(include_str!("../wasm/finite_loop.wat")),
    },
    Sample {
        id: "infinite_loop",
        description: "无限循环：仅消耗燃料，必须被燃料耗尽终止，不提交。",
        source: SampleSource::Wat(include_str!("../wasm/infinite_loop.wat")),
    },
    Sample {
        id: "trap_after_write",
        description: "先写 KV 再 unreachable：陷阱后写入必须回滚。",
        source: SampleSource::Wat(include_str!("../wasm/trap_after_write.wat")),
    },
    Sample {
        id: "memory_grow",
        description: "按输入页数增长内存：限制内成功并触碰新页，超限即终止不提交。",
        source: SampleSource::Wat(include_str!("../wasm/memory_grow.wat")),
    },
    Sample {
        id: "host_oob",
        description: "向宿主传入越界指针：宿主边界检查陷阱，写入回滚。",
        source: SampleSource::Wat(include_str!("../wasm/host_oob.wat")),
    },
    Sample {
        id: "frame_protocol",
        description: "协议操作：长度前缀分帧 + 真实 CRC-32 校验往返。",
        source: SampleSource::Wat(include_str!("../wasm/frame_protocol.wat")),
    },
    Sample {
        id: "sha256guest",
        description: "Rust(no_std) 来宾：真实链式 SHA-256 密码学计算，多轮迭代可耗尽燃料。",
        source: SampleSource::Wasm(include_bytes!("../wasm/prebuilt/sha256guest.wasm")),
    },
];

pub fn by_id(id: &str) -> Option<&'static Sample> {
    SAMPLES.iter().find(|s| s.id == id)
}

pub fn compile(sample: &Sample) -> anyhow::Result<Vec<u8>> {
    match sample.source {
        SampleSource::Wat(wat) => Ok(wat::parse_str(wat)?),
        SampleSource::Wasm(bytes) => Ok(bytes.to_vec()),
    }
}
