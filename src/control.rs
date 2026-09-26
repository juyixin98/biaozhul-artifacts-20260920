//! JSON 控制入口：把 JSON 请求分发到编码 / 解码 / 合并 / 检视操作。
//!
//! 所有二进制 `.cdc` 数据在 JSON 中以 base64 字符串承载（字段 `data_b64` /
//! `inputs_b64`）。这是给脚本和集成使用的控制面；面向大批量场景应直接调用库的
//! 流式 API（`FileWriter` / `FileReader`）。

use std::io::BufReader;

use crate::error::{Error, Result};
use crate::json::{self, Value};
use crate::merge::{merge_sources, MergeLimits, MergeOptions, MergeStats};
use crate::reader::{DecodeLimits, FileReader, ReadStats, SegmentData};
use crate::writer::{EncodeLimits, FileWriter, WriteStats};
use crate::{base64, MAGIC};

/// 处理一个 JSON 请求字符串，返回紧凑 JSON 响应。
/// 任何错误都会被转成 `{"ok":false,...}`，不会 panic。
pub fn handle(request_text: &str) -> String {
    match handle_inner(request_text) {
        Ok(v) => json::to_string(&v),
        Err(e) => error_response(&e),
    }
}

/// 同 [`handle`]，但输出带缩进的 JSON。
pub fn handle_pretty(request_text: &str) -> String {
    match handle_inner(request_text) {
        Ok(v) => json::to_string_pretty(&v, 2),
        Err(e) => {
            let raw = error_response(&e);
            json::to_string_pretty(&json::parse(&raw).unwrap_or(Value::Null), 2)
        }
    }
}

fn handle_inner(text: &str) -> Result<Value> {
    let req = json::parse(text)?;
    let op = req
        .get("op")
        .and_then(Value::as_str)
        .ok_or_else(|| Error::BadRequest("missing or non-string field 'op'".into()))?;
    match op {
        "encode" => op_encode(&req),
        "decode" => op_decode(&req),
        "merge" => op_merge(&req),
        "inspect" => op_inspect(&req),
        other => Err(Error::BadRequest(format!("unknown op '{other}'"))),
    }
}

// ---------- encode ----------

fn op_encode(req: &Value) -> Result<Value> {
    let mut limits = parse_encode_limits(req.get("limits"))?;
    if let Some(n) = req.get("rows_per_segment").and_then(Value::as_u64) {
        limits.max_rows_per_segment = n;
    }

    let mut out: Vec<u8> = Vec::new();
    let mut writer = FileWriter::new(&mut out, limits)?;

    // 两种输入形式二选一：
    // - "rows": ["a", null, ...]            自动按 rows_per_segment 分段；
    // - "segments": [["a","b"], [null,"c"]] 每个子数组强制为一个段。
    let stats = if let Some(segs) = req.get("segments").and_then(Value::as_array) {
        if req.get("rows").is_some() {
            return Err(Error::BadRequest(
                "provide either 'rows' or 'segments', not both".into(),
            ));
        }
        for seg in segs {
            let items = seg
                .as_array()
                .ok_or_else(|| Error::BadRequest("each segment must be an array".into()))?;
            for item in items {
                write_json_row(&mut writer, item)?;
            }
            writer.finish_segment()?; // 显式段边界
        }
        writer.finish()?
    } else {
        let rows = req
            .get("rows")
            .and_then(Value::as_array)
            .ok_or_else(|| Error::BadRequest("missing array field 'rows' or 'segments'".into()))?;
        for item in rows {
            write_json_row(&mut writer, item)?;
        }
        writer.finish()?
    };

    Ok(object(vec![
        ("ok", Value::Bool(true)),
        ("data_b64", Value::Str(base64::encode(&out))),
        (
            "magic",
            Value::Str(String::from_utf8(MAGIC.to_vec()).unwrap()),
        ),
        ("write_stats", write_stats_json(&stats)),
    ]))
}

fn write_json_row(writer: &mut FileWriter<&mut Vec<u8>>, item: &Value) -> Result<()> {
    match item {
        Value::Null => writer.write_row(None),
        Value::Str(s) => writer.write_row(Some(s)),
        _ => Err(Error::BadRequest(
            "each row must be a string or null".into(),
        )),
    }
}

// ---------- decode ----------

fn op_decode(req: &Value) -> Result<Value> {
    let data = take_data_b64(req)?;
    let limits = parse_decode_limits(req.get("limits"))?;
    let reader = FileReader::new(BufReader::new(data.as_slice()), limits)?;
    let (segments, rows, stats, value_bytes) = read_all(reader)?;

    let rows_json = Value::Arr(
        rows.into_iter()
            .map(|opt| match opt {
                None => Value::Null,
                Some(s) => Value::Str(s),
            })
            .collect(),
    );
    Ok(object(vec![
        ("ok", Value::Bool(true)),
        ("rows", rows_json),
        ("segments", segments_json(&segments)),
        ("read_stats", read_stats_json(&stats, value_bytes)),
    ]))
}

// ---------- merge ----------

fn op_merge(req: &Value) -> Result<Value> {
    let inputs = req
        .get("inputs_b64")
        .and_then(Value::as_array)
        .ok_or_else(|| Error::BadRequest("missing array field 'inputs_b64'".into()))?;
    if inputs.is_empty() {
        return Err(Error::BadRequest(
            "'inputs_b64' must contain at least one file".into(),
        ));
    }

    let mut all_segments: Vec<SegmentData> = Vec::new();
    for inp in inputs {
        let b64 = inp
            .as_str()
            .ok_or_else(|| Error::BadRequest("each input must be a base64 string".into()))?;
        let data = base64::decode(b64)?;
        let reader = FileReader::new(BufReader::new(data.as_slice()), DecodeLimits::default())?;
        let (segs, _, _, _) = read_all(reader)?;
        all_segments.extend(segs);
    }

    let options = MergeOptions {
        limits: parse_merge_limits(req.get("limits"))?,
        canonical: req
            .get("canonical")
            .and_then(Value::as_bool)
            .unwrap_or(false),
    };
    let (merged, stats) = merge_sources(&all_segments, &options)?;

    Ok(object(vec![
        ("ok", Value::Bool(true)),
        ("data_b64", Value::Str(base64::encode(&merged))),
        ("merge_stats", merge_stats_json(&stats)),
    ]))
}

// ---------- inspect ----------

fn op_inspect(req: &Value) -> Result<Value> {
    let data = take_data_b64(req)?;
    let limits = parse_decode_limits(req.get("limits"))?;
    let reader = FileReader::new(BufReader::new(data.as_slice()), limits)?;
    let (segments, _, stats, value_bytes) = read_all(reader)?;
    Ok(object(vec![
        ("ok", Value::Bool(true)),
        (
            "magic",
            Value::Str(String::from_utf8(MAGIC.to_vec()).unwrap()),
        ),
        ("version", Value::Num(crate::FORMAT_VERSION.to_string())),
        ("segments", segments_json(&segments)),
        ("read_stats", read_stats_json(&stats, value_bytes)),
    ]))
}

// ---------- 读取辅助 ----------

fn take_data_b64(req: &Value) -> Result<Vec<u8>> {
    let s = req
        .get("data_b64")
        .and_then(Value::as_str)
        .ok_or_else(|| Error::BadRequest("missing string field 'data_b64'".into()))?;
    base64::decode(s)
}

/// read_all 的收集结果：段列表、逐行值、行/NULL/段统计、输出字节总数。
type ReadAll = (Vec<SegmentData>, Vec<Option<String>>, ReadStats, u64);

/// 读取并克隆全部段，同时逐行还原值，累计输出字节并套用 `max_value_bytes`。
fn read_all<R: std::io::BufRead>(mut reader: FileReader<R>) -> Result<ReadAll> {
    let max_value_bytes = reader.limits_ref().max_value_bytes;
    let mut segments: Vec<SegmentData> = Vec::new();
    let mut rows: Vec<Option<String>> = Vec::new();
    let mut value_bytes: u64 = 0;

    while let Some(seg) = reader.next_segment()? {
        for r in 0..seg.row_count() {
            match seg.row_bytes(r) {
                Some(None) => rows.push(None),
                Some(Some(b)) => {
                    value_bytes = value_bytes.saturating_add(b.len() as u64);
                    if value_bytes > max_value_bytes {
                        return Err(Error::ValueBytesLimitExceeded {
                            limit: max_value_bytes,
                        });
                    }
                    let s = std::str::from_utf8(b)
                        .map_err(|_| Error::BadUtf8)?
                        .to_owned();
                    rows.push(Some(s));
                }
                None => unreachable!("row index in range by construction"),
            }
        }
        segments.push(seg.clone());
    }
    Ok((segments, rows, reader.stats().clone(), value_bytes))
}

// ---------- limits / JSON 组装 ----------

fn parse_encode_limits(v: Option<&Value>) -> Result<EncodeLimits> {
    let mut l = EncodeLimits::default();
    let Some(v) = v else { return Ok(l) };
    if let Some(n) = v.get("max_segment_bytes").and_then(Value::as_u64) {
        l.max_segment_bytes = n as usize;
    }
    if let Some(n) = v.get("max_rows_per_segment").and_then(Value::as_u64) {
        l.max_rows_per_segment = n;
    }
    if let Some(n) = v.get("max_value_len").and_then(Value::as_u64) {
        l.max_value_len = n;
    }
    if let Some(n) = v.get("max_dict_cardinality").and_then(Value::as_u64) {
        l.max_dict_cardinality = n as usize;
    }
    Ok(l)
}

fn parse_decode_limits(v: Option<&Value>) -> Result<DecodeLimits> {
    let mut l = DecodeLimits::default();
    let Some(v) = v else { return Ok(l) };
    if let Some(n) = v.get("max_segment_bytes").and_then(Value::as_u64) {
        l.max_segment_bytes = n;
    }
    if let Some(n) = v.get("max_rows").and_then(Value::as_u64) {
        l.max_rows = n;
    }
    if let Some(n) = v.get("max_value_bytes").and_then(Value::as_u64) {
        l.max_value_bytes = n;
    }
    if let Some(n) = v.get("max_dict_bytes").and_then(Value::as_u64) {
        l.max_dict_bytes = n as usize;
    }
    if let Some(n) = v.get("max_cardinality").and_then(Value::as_u64) {
        l.max_cardinality = n as usize;
    }
    if let Some(n) = v.get("max_segments").and_then(Value::as_u64) {
        l.max_segments = n;
    }
    Ok(l)
}

fn parse_merge_limits(v: Option<&Value>) -> Result<MergeLimits> {
    let mut l = MergeLimits::default();
    let Some(v) = v else { return Ok(l) };
    if let Some(n) = v.get("max_distinct").and_then(Value::as_u64) {
        l.max_distinct = n as usize;
    }
    if let Some(n) = v.get("max_dict_bytes").and_then(Value::as_u64) {
        l.max_dict_bytes = n as usize;
    }
    if let Some(n) = v.get("max_rows").and_then(Value::as_u64) {
        l.max_rows = n;
    }
    Ok(l)
}

fn object(entries: Vec<(&str, Value)>) -> Value {
    Value::Obj(
        entries
            .into_iter()
            .map(|(k, v)| (k.to_owned(), v))
            .collect(),
    )
}

fn segments_json(segments: &[SegmentData]) -> Value {
    Value::Arr(
        segments
            .iter()
            .map(|seg| {
                object(vec![
                    ("segment_no", Value::Num(seg.segment_no().to_string())),
                    ("rows", Value::Num(seg.row_count().to_string())),
                    ("nulls", Value::Num(seg.null_positions().len().to_string())),
                    ("cardinality", Value::Num(seg.cardinality().to_string())),
                ])
            })
            .collect(),
    )
}

fn write_stats_json(s: &WriteStats) -> Value {
    object(vec![
        ("segments", Value::Num(s.segments.to_string())),
        ("rows", Value::Num(s.rows.to_string())),
        ("nulls", Value::Num(s.nulls.to_string())),
        ("distinct_sum", Value::Num(s.distinct_sum.to_string())),
        ("bytes_written", Value::Num(s.bytes_written.to_string())),
    ])
}

fn read_stats_json(s: &ReadStats, value_bytes: u64) -> Value {
    object(vec![
        ("segments", Value::Num(s.segments.to_string())),
        ("rows", Value::Num(s.rows.to_string())),
        ("nulls", Value::Num(s.nulls.to_string())),
        ("value_bytes", Value::Num(value_bytes.to_string())),
    ])
}

fn merge_stats_json(s: &MergeStats) -> Value {
    object(vec![
        ("input_segments", Value::Num(s.input_segments.to_string())),
        ("rows", Value::Num(s.rows.to_string())),
        ("nulls", Value::Num(s.nulls.to_string())),
        ("global_distinct", Value::Num(s.global_distinct.to_string())),
        (
            "local_distinct_sum",
            Value::Num(s.local_distinct_sum.to_string()),
        ),
        ("deduped", Value::Num(s.deduped.to_string())),
    ])
}

fn error_response(e: &Error) -> String {
    let code = error_code(e);
    let body = object(vec![
        ("ok", Value::Bool(false)),
        (
            "error",
            object(vec![
                ("code", Value::Str(code.to_owned())),
                ("message", Value::Str(e.to_string())),
            ]),
        ),
    ]);
    json::to_string(&body)
}

fn error_code(e: &Error) -> &'static str {
    match e {
        Error::BadMagic
        | Error::UnsupportedVersion(_)
        | Error::BadFlags(_)
        | Error::BadVarint
        | Error::BadUtf8
        | Error::BadSegmentKind(_)
        | Error::BadSegment(_)
        | Error::DuplicateDictEntry
        | Error::BadSegmentOrder(_) => "corrupt_or_unsupported_format",
        Error::UnexpectedEof { .. } => "truncated_input",
        Error::DictIdOutOfRange { .. } | Error::CardinalityTooLarge { .. } => "dictionary_error",
        Error::TooManySegments { .. }
        | Error::RowsLimitExceeded { .. }
        | Error::ValueBytesLimitExceeded { .. }
        | Error::MemoryLimitExceeded { .. }
        | Error::DictBytesLimitExceeded { .. }
        | Error::SegmentTooLarge { .. } => "limit_exceeded",
        Error::BadJson(_) => "bad_json",
        Error::BadRequest(_) => "bad_request",
        Error::BadBase64(_) => "bad_base64",
        Error::CrcMismatch { .. } => "crc_mismatch",
        Error::Io(_) => "io_error",
    }
}
