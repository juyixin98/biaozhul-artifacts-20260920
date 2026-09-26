//! JSON control entry point.
//!
//! Every operation is expressed as one JSON request and returns one JSON
//! response (or a structured error), so the library can be driven from the
//! CLI, a pipe, or embedded behind any transport without a frontend:
//!
//! | op          | purpose                                            |
//! |-------------|----------------------------------------------------|
//! | `encode`    | schema + message JSON -> BSE1 bytes                |
//! | `decode`    | BSE1 bytes + schema -> message JSON + stats        |
//! | `forward`   | decode, apply a field patch, re-encode; preserves unknown fields |
//! | `compat`    | compare an old and a new schema                    |
//! | `truncate`  | cut BSE1 bytes to N payload bytes (for tests)      |

use crate::codec::{decode_envelope, encode_envelope, DecodeStats, EncodeLimit};
use crate::error::{Error, Result};
use crate::json::{JsonObject, JsonValue};
use crate::jsonio::{
    data_from_request, hex_encode, limits_from_json, message_from_json, message_to_json,
    schema_from_json,
};
use crate::schema::{check_message_evolution, Schema};
use crate::value::{Field, Message};

/// Successful response envelope: `{"ok": true, "result": {...}}`.
fn ok(result: JsonValue) -> JsonValue {
    let mut o = JsonObject::new();
    o.set("ok", JsonValue::Bool(true));
    o.set("result", result);
    JsonValue::Object(o)
}

/// Error response envelope: `{"ok": false, "error": "..."}`.
pub fn error_response(e: &Error) -> JsonValue {
    let mut o = JsonObject::new();
    o.set("ok", JsonValue::Bool(false));
    o.set("error", JsonValue::Str(e.to_string()));
    JsonValue::Object(o)
}

/// Execute one JSON request.
pub fn execute(request: &JsonValue) -> Result<JsonValue> {
    let obj = request
        .as_object()
        .ok_or_else(|| Error::InvalidInput("request must be a JSON object".into()))?;
    let op = obj
        .get("op")
        .and_then(JsonValue::as_str)
        .ok_or_else(|| Error::InvalidInput("request needs a string 'op'".into()))?;
    match op {
        "encode" => op_encode(obj),
        "decode" => op_decode(obj),
        "forward" => op_forward(obj),
        "compat" => op_compat(obj),
        "truncate" => op_truncate(obj),
        other => Err(Error::InvalidInput(format!(
            "unknown op '{other}'; expected encode|decode|forward|compat|truncate"
        ))),
    }
}

fn schema_field(obj: &JsonObject, key: &str) -> Result<Schema> {
    let doc = obj
        .get(key)
        .ok_or_else(|| Error::InvalidInput(format!("missing '{key}'")))?;
    schema_from_json(doc)
}

fn encode_limit(obj: &JsonObject) -> EncodeLimit {
    let mut limit = EncodeLimit::default();
    if let Some(JsonValue::Int(n)) = obj.get("max_output") {
        if *n >= 0 {
            limit.max_output = *n as u64;
        }
    }
    limit
}

fn op_encode(obj: &JsonObject) -> Result<JsonValue> {
    let schema = schema_field(obj, "schema")?;
    let message_doc = obj
        .get("message")
        .ok_or_else(|| Error::InvalidInput("encode requires 'message'".into()))?;
    let message = message_from_json(&schema, message_doc)?;
    let limit = encode_limit(obj);

    let mut bytes = Vec::new();
    let written = encode_envelope(&schema, &message, &mut bytes, limit)?;

    let mut result = JsonObject::new();
    result.set(
        "data_b64",
        JsonValue::Str(crate::json::base64_encode(&bytes)),
    );
    result.set("data_hex", JsonValue::Str(hex_encode(&bytes)));
    result.set("bytes_total", JsonValue::Int(bytes.len() as i64));
    result.set("payload_bytes", JsonValue::Int(written as i64));
    Ok(ok(JsonValue::Object(result)))
}

fn op_decode(obj: &JsonObject) -> Result<JsonValue> {
    let schema = schema_field(obj, "schema")?;
    let data = data_from_request(obj)?;
    let limits = limits_from_json(obj);
    let mut cursor = std::io::Cursor::new(data);
    let decoded = decode_envelope(&schema, &mut cursor, limits)?;
    Ok(ok(decode_result(&schema, decoded.message, decoded.stats)))
}

fn decode_result(schema: &Schema, message: Message, stats: DecodeStats) -> JsonValue {
    let mut result = JsonObject::new();
    result.set("message", message_to_json(schema, &message));
    result.set("stats", stats_to_json(&stats));
    JsonValue::Object(result)
}

fn stats_to_json(stats: &DecodeStats) -> JsonValue {
    let mut o = JsonObject::new();
    o.set("fields_seen", JsonValue::Int(stats.fields_seen as i64));
    o.set(
        "unknown_fields",
        JsonValue::Int(stats.unknown_fields as i64),
    );
    o.set("unknown_bytes", JsonValue::Int(stats.unknown_bytes as i64));
    JsonValue::Object(o)
}

/// Forward: decode under `schema`, optionally overwrite fields listed in
/// `set` (a message JSON document; omitted keys are left untouched and
/// unknown wire fields are preserved), then re-encode.
fn op_forward(obj: &JsonObject) -> Result<JsonValue> {
    let schema = schema_field(obj, "schema")?;
    let data = data_from_request(obj)?;
    let limits = limits_from_json(obj);
    let limit = encode_limit(obj);

    let mut cursor = std::io::Cursor::new(data);
    let decoded = decode_envelope(&schema, &mut cursor, limits)?;
    let mut message = decoded.message;

    if let Some(patch_doc) = obj.get("set") {
        let patch = message_from_json(&schema, patch_doc)?;
        apply_patch(&schema, &mut message, patch)?;
    }

    let mut out = Vec::new();
    let written = encode_envelope(&schema, &message, &mut out, limit)?;

    let mut result = JsonObject::new();
    result.set("data_b64", JsonValue::Str(crate::json::base64_encode(&out)));
    result.set("data_hex", JsonValue::Str(hex_encode(&out)));
    result.set("bytes_total", JsonValue::Int(out.len() as i64));
    result.set("payload_bytes", JsonValue::Int(written as i64));
    result.set("message", message_to_json(&schema, &message));
    result.set("stats", stats_to_json(&decoded.stats));
    Ok(ok(JsonValue::Object(result)))
}

/// Merge `patch` into `message`:
///
/// * `Field::Missing` in the patch leaves the current value untouched;
/// * `Field::Present` / `Field::Repeated` replace that field;
/// * unknown fields captured at decode time are never removed here.
fn apply_patch(schema: &Schema, message: &mut Message, patch: Message) -> Result<()> {
    for number in schema.root_message().fields.keys() {
        if let Some(patch_field) = patch.fields.get(number) {
            match patch_field {
                Field::Missing => { /* explicit no-op */ }
                other => {
                    message.fields.insert(*number, other.clone());
                }
            }
        }
    }
    Ok(())
}

fn op_compat(obj: &JsonObject) -> Result<JsonValue> {
    let old_schema = schema_field(obj, "old_schema")?;
    let new_schema = schema_field(obj, "new_schema")?;

    // Compare every message type that exists in either bundle under the
    // same name; the primary verdict uses the old/new root types.
    let mut names: Vec<String> = old_schema.messages.keys().cloned().collect();
    for name in new_schema.messages.keys() {
        if !names.contains(name) {
            names.push(name.clone());
        }
    }
    names.sort();

    let mut all_compatible = true;
    let mut reports = Vec::new();
    for name in names {
        let empty = crate::schema::MessageDef {
            name: name.clone(),
            fields: Default::default(),
        };
        let old = old_schema.messages.get(&name).unwrap_or(&empty);
        let new = new_schema.messages.get(&name).unwrap_or(&empty);
        let report = check_message_evolution(&name, old, new);
        if !report.compatible {
            all_compatible = false;
        }
        reports.push(report_to_json(&report));
    }

    let mut result = JsonObject::new();
    result.set("compatible", JsonValue::Bool(all_compatible));
    let direction = if all_compatible {
        "old<->new: fields present on only one side are added-optionals or removed-into-unknown"
            .to_string()
    } else {
        "incompatible: at least one field cannot be read across versions".to_string()
    };
    result.set("direction", JsonValue::Str(direction));
    result.set("reports", JsonValue::Array(reports));
    Ok(ok(JsonValue::Object(result)))
}

fn report_to_json(report: &crate::schema::EvolutionReport) -> JsonValue {
    let mut o = JsonObject::new();
    o.set("message", JsonValue::Str(report.message.clone()));
    o.set("compatible", JsonValue::Bool(report.compatible));
    let fields: Vec<JsonValue> = report
        .notes
        .iter()
        .map(|(number, name, verdict)| {
            let (status, detail) = match verdict {
                crate::schema::EvolutionVerdict::Compatible => ("compatible", String::new()),
                crate::schema::EvolutionVerdict::Directional(d) => ("directional", d.clone()),
                crate::schema::EvolutionVerdict::Incompatible(d) => ("incompatible", d.clone()),
            };
            let mut fo = JsonObject::new();
            fo.set("number", JsonValue::Int(*number as i64));
            fo.set("name", JsonValue::Str(name.clone()));
            fo.set("verdict", JsonValue::Str(status.into()));
            if !detail.is_empty() {
                fo.set("detail", JsonValue::Str(detail));
            }
            JsonValue::Object(fo)
        })
        .collect();
    o.set("fields", JsonValue::Array(fields));
    JsonValue::Object(o)
}

/// Cut an envelope's payload to `keep` bytes (keeping the header) so the
/// truncated data can be fed straight back into `decode` to observe the
/// resulting error. `keep` counts payload bytes; pass `keep_header: true`
/// to also truncate only within the payload.
fn op_truncate(obj: &JsonObject) -> Result<JsonValue> {
    let data = data_from_request(obj)?;
    let keep = obj
        .get("keep")
        .and_then(JsonValue::as_int)
        .and_then(|n| if n >= 0 { Some(n as u64) } else { None })
        .ok_or_else(|| {
            Error::InvalidInput("truncate needs a non-negative integer 'keep'".into())
        })?;

    const HEADER_LEN: usize = 10; // "BSE1" + version + flags + u32 len
    if data.len() < HEADER_LEN || data[..4] != crate::wire::ENVELOPE_MAGIC {
        return Err(Error::InvalidInput("not a BSE1 envelope".into()));
    }
    let declared = u32::from_le_bytes([data[6], data[7], data[8], data[9]]) as u64;
    let kept = keep.min(declared);
    let mut out = data[..HEADER_LEN].to_vec();
    out.extend_from_slice(&data[HEADER_LEN..HEADER_LEN + kept as usize]);

    let mut result = JsonObject::new();
    result.set("data_b64", JsonValue::Str(crate::json::base64_encode(&out)));
    result.set("data_hex", JsonValue::Str(hex_encode(&out)));
    result.set("declared_payload_bytes", JsonValue::Int(declared as i64));
    result.set("kept_payload_bytes", JsonValue::Int(kept as i64));
    Ok(ok(JsonValue::Object(result)))
}
