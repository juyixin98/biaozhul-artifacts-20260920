//! `utf8ctl` — JSON control entry point for the incremental UTF-8 decoder.
//!
//! Protocol: newline-delimited JSON. Each line on stdin is one request
//! object; each request produces exactly one response object on stdout.
//! The decoder is stateful across requests within one process run, which
//! is what makes cross-chunk (incremental) validation drivable from the
//! outside. See README.md for the full protocol documentation.

use std::io::{BufRead, Write};

use inc_utf8::decoder::{IncrementalDecoder, Limits, Recovery};
use inc_utf8::encoder;
use inc_utf8::error::DecodeError;
use inc_utf8::hex;
use inc_utf8::json::{parse, Json};

fn main() {
    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();

    let mut limits = Limits::default();
    let mut recovery = Recovery::FailFast;
    let mut decoder = IncrementalDecoder::new(limits, recovery);

    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(e) => {
                eprintln!("utf8ctl: failed to read stdin: {e}");
                std::process::exit(2);
            }
        };
        if line.trim().is_empty() {
            continue;
        }
        let response = match parse(&line) {
            Ok(request) => handle(&request, &mut decoder, &mut limits, &mut recovery),
            Err(e) => Json::obj(vec![
                ("id", Json::Null),
                ("ok", Json::Bool(false)),
                ("error", Json::Str(format!("invalid JSON request: {e}"))),
            ]),
        };
        let mut text = response.to_json_string();
        text.push('\n');
        if out.write_all(text.as_bytes()).is_err() || out.flush().is_err() {
            // Client went away; nothing useful left to do.
            return;
        }
    }
}

fn handle(
    request: &Json,
    decoder: &mut IncrementalDecoder,
    limits: &mut Limits,
    recovery: &mut Recovery,
) -> Json {
    let id = request.get("id").cloned().unwrap_or(Json::Null);
    let reply = |ok: bool, extra: Vec<(&str, Json)>| {
        let mut members = vec![("id", id.clone()), ("ok", Json::Bool(ok))];
        members.extend(extra);
        Json::obj(members)
    };

    let op = match request.get("op").and_then(Json::as_str) {
        Some(op) => op,
        None => {
            return reply(
                false,
                vec![("error", Json::Str("missing \"op\" field".into()))],
            )
        }
    };

    match op {
        "configure" => {
            if decoder.total_consumed() > 0 {
                return reply(
                    false,
                    vec![(
                        "error",
                        Json::Str(
                            "configure is only allowed before any decode or after reset".into(),
                        ),
                    )],
                );
            }
            if let Some(l) = request.get("limits") {
                if let Some(n) = l.get("max_input_bytes").and_then(Json::as_u64) {
                    limits.max_input_bytes = n;
                }
                if let Some(n) = l.get("max_output_codepoints").and_then(Json::as_u64) {
                    limits.max_output_codepoints = n;
                }
            }
            if let Some(r) = request.get("recovery").and_then(Json::as_str) {
                match r {
                    "fail_fast" => *recovery = Recovery::FailFast,
                    "skip" => *recovery = Recovery::SkipInvalidBytes,
                    _ => {
                        return reply(
                            false,
                            vec![(
                                "error",
                                Json::Str(format!("unknown recovery policy \"{r}\"")),
                            )],
                        )
                    }
                }
            }
            *decoder = IncrementalDecoder::new(*limits, *recovery);
            reply(
                true,
                vec![
                    ("max_input_bytes", Json::num(limits.max_input_bytes)),
                    (
                        "max_output_codepoints",
                        Json::num(limits.max_output_codepoints),
                    ),
                    (
                        "recovery",
                        Json::Str(match recovery {
                            Recovery::FailFast => "fail_fast".into(),
                            Recovery::SkipInvalidBytes => "skip".into(),
                        }),
                    ),
                ],
            )
        }
        "decode" => {
            let data_hex = match request.get("data_hex").and_then(Json::as_str) {
                Some(h) => h,
                None => {
                    return reply(
                        false,
                        vec![("error", Json::Str("missing \"data_hex\"".into()))],
                    )
                }
            };
            let bytes = match hex::from_hex(data_hex) {
                Ok(b) => b,
                Err(e) => return reply(false, vec![("error", Json::Str(e))]),
            };
            let outcome = decoder.feed(&bytes);
            let errors = errors_json(&outcome.errors);
            let output_hex = encoder::encode_all(&outcome.codepoints)
                .map(|b| hex::to_hex(&b))
                .unwrap_or_default();
            reply(
                outcome.errors.is_empty(),
                vec![
                    (
                        "codepoints",
                        Json::Arr(
                            outcome
                                .codepoints
                                .iter()
                                .map(|&cp| Json::num(cp as u64))
                                .collect(),
                        ),
                    ),
                    ("output_hex", Json::Str(output_hex)),
                    ("errors", errors),
                    ("stopped", Json::Bool(outcome.stopped)),
                    ("consumed", Json::num(decoder.total_consumed())),
                    ("emitted", Json::num(decoder.total_emitted())),
                ],
            )
        }
        "finish" => {
            let errors: Vec<DecodeError> = decoder.finish().into_iter().collect();
            reply(
                errors.is_empty(),
                vec![
                    ("errors", errors_json(&errors)),
                    ("consumed", Json::num(decoder.total_consumed())),
                    ("emitted", Json::num(decoder.total_emitted())),
                ],
            )
        }
        "encode" => {
            let cps = match request.get("codepoints").and_then(Json::as_array) {
                Some(a) => a,
                None => {
                    return reply(
                        false,
                        vec![("error", Json::Str("missing \"codepoints\" array".into()))],
                    )
                }
            };
            let mut values = Vec::with_capacity(cps.len());
            for item in cps {
                match item.as_u64() {
                    Some(n) if n <= u32::MAX as u64 => values.push(n as u32),
                    _ => {
                        return reply(
                            false,
                            vec![(
                                "error",
                                Json::Str("codepoints must be non-negative integers".into()),
                            )],
                        )
                    }
                }
            }
            match encoder::encode_all(&values) {
                Ok(bytes) => reply(true, vec![("data_hex", Json::Str(hex::to_hex(&bytes)))]),
                Err(e) => reply(false, vec![("error", Json::Str(e.to_string()))]),
            }
        }
        "reset" => {
            decoder.reset();
            reply(true, vec![])
        }
        "stats" => reply(
            true,
            vec![
                ("consumed", Json::num(decoder.total_consumed())),
                ("emitted", Json::num(decoder.total_emitted())),
                ("pending_bytes", Json::num(decoder.pending_bytes() as u64)),
            ],
        ),
        other => reply(
            false,
            vec![("error", Json::Str(format!("unknown op \"{other}\"")))],
        ),
    }
}

fn errors_json(errors: &[DecodeError]) -> Json {
    Json::Arr(
        errors
            .iter()
            .map(|e| {
                Json::obj(vec![
                    ("kind", Json::Str(e.kind.as_str().into())),
                    ("offset", Json::num(e.offset)),
                    ("sequence_start", Json::num(e.sequence_start)),
                ])
            })
            .collect(),
    )
}
