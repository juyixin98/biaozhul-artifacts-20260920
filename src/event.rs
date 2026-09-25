//! 流式解析输出的事件。

use std::collections::BTreeMap;

/// 一个 part 的元数据（头解析完成时产生）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PartMeta {
    /// `Content-Disposition` 里的 `name` 参数（未转义后的原值）。
    pub name: String,
    /// `Content-Disposition` 里的 `filename` 参数（未转义后的原值），没有则为 `None`。
    pub filename: Option<String>,
    /// 其余头：头名统一小写，值为 trim 后的原样字符串；同名多头保留第一次出现的值。
    pub headers: BTreeMap<String, String>,
}

/// 增量解析事件。
///
/// 一个普通 part 的事件序列为：
/// `PartBegin` → `Body(...)`*（可能分多次）→ `PartEnd`；
/// 空 part 为 `PartBegin` → `PartEnd`（中间没有 `Body`）。
/// 整个流结束时 `finish()` 额外产生一个 `End`。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Event {
    /// 一个新 part 的头解析完成。
    PartBegin(PartMeta),
    /// 一段正文数据。**不做**任何解码/转码，原样字节透传（二进制安全）。
    Body(Vec<u8>),
    /// 当前 part 结束（遇到非关闭的边界分隔行）。
    PartEnd,
    /// 遇到关闭边界 `--boundary--`，整个 multipart 流正常结束。
    End,
}
