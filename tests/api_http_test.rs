//! End-to-end HTTP tests: real Axum router on an ephemeral port, real JSON
//! over TCP, asserting on bodies, status codes, request-id correlation and
//! error categories — never just "the endpoint is callable".

mod common;

use decorrelate::api::http::router;
use decorrelate::catalog::Catalog;
use serde_json::{json, Value};

async fn spawn_server() -> (String, reqwest_free::ClientGuard) {
    // Avoid adding an HTTP-client dependency: talk raw HTTP over TCP.
    let catalog = Catalog::load_from_dir("fixtures/tables").expect("fixtures load");
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    listener.set_nonblocking(true).unwrap();
    let listener = tokio::net::TcpListener::from_std(listener).unwrap();
    tokio::spawn(async move {
        let _ = axum::serve(listener, router(catalog)).await;
    });
    (format!("http://127.0.0.1:{}", addr.port()), reqwest_free::ClientGuard)
}

/// Minimal JSON-over-HTTP helper (no extra test dependency). Reads the body
/// via Content-Length / chunked framing rather than relying on the server
/// closing a keep-alive connection.
mod reqwest_free {
    use std::io::{BufRead, BufReader, Read, Write};
    use std::net::TcpStream;
    use std::time::Duration;

    pub struct ClientGuard;

    pub struct Response {
        pub status: u16,
        pub header_request_id: Option<String>,
        pub body: serde_json::Value,
    }

    pub fn post(base: &str, path: &str, body: &str, request_id: Option<&str>) -> Response {
        let addr = base.strip_prefix("http://").unwrap();
        let mut stream = connect(addr);
        write!(
            stream,
            "POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {len}\r\n{rid}\r\n",
            len = body.len(),
            rid = request_id.map(|id| format!("X-Request-Id: {id}\r\n")).unwrap_or_default()
        )
        .unwrap();
        stream.write_all(body.as_bytes()).unwrap();
        stream.flush().unwrap();
        read_response(stream)
    }

    pub fn get(base: &str, path: &str) -> Response {
        let addr = base.strip_prefix("http://").unwrap();
        let mut stream = connect(addr);
        write!(stream, "GET {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n").unwrap();
        stream.flush().unwrap();
        read_response(stream)
    }

    fn connect(addr: &str) -> TcpStream {
        let mut last_err = None;
        // The spawned server may not be accepting yet; retry briefly.
        for _ in 0..50 {
            match TcpStream::connect(addr) {
                Ok(s) => {
                    s.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
                    return s;
                }
                Err(e) => {
                    last_err = Some(e);
                    std::thread::sleep(Duration::from_millis(20));
                }
            }
        }
        panic!("could not connect to {addr}: {:?}", last_err);
    }

    fn read_response(stream: TcpStream) -> Response {
        let mut reader = BufReader::new(stream);
        let mut status_line = String::new();
        reader.read_line(&mut status_line).unwrap();
        let status = status_line.split_whitespace().nth(1).unwrap().parse().unwrap();

        let mut header_request_id = None;
        let mut content_length: Option<usize> = None;
        let mut chunked = false;
        loop {
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            if line == "\r\n" || line == "\n" || line.is_empty() {
                break;
            }
            let (name, value) = line.split_once(':').unwrap();
            let value = value.trim();
            if name.eq_ignore_ascii_case("x-request-id") {
                header_request_id = Some(value.to_string());
            } else if name.eq_ignore_ascii_case("content-length") {
                content_length = Some(value.parse().unwrap());
            } else if name.eq_ignore_ascii_case("transfer-encoding") && value.to_ascii_lowercase().contains("chunked") {
                chunked = true;
            }
        }

        let body_text = if let Some(len) = content_length {
            let mut buf = vec![0u8; len];
            reader.read_exact(&mut buf).unwrap();
            String::from_utf8(buf).unwrap()
        } else if chunked {
            dechunk(&mut reader)
        } else {
            // No body framing (e.g. empty response): drain until close/EOF.
            let mut buf = String::new();
            let _ = reader.read_to_string(&mut buf);
            buf
        };

        let body = serde_json::from_str(body_text.trim()).unwrap_or_else(|e| {
            panic!("response body is JSON: {e}\nbody: {body_text}")
        });
        Response { status, header_request_id, body }
    }

    fn dechunk<R: Read>(reader: &mut R) -> String {
        let mut out = Vec::new();
        loop {
            let size = read_chunk_size_line(reader);
            if size == 0 {
                // Consume the terminating CRLF after the zero-length chunk
                // (plus any trailer section up to the blank line).
                let mut crlf = [0u8; 2];
                let _ = reader.read_exact(&mut crlf);
                break;
            }
            let mut chunk = vec![0u8; size];
            reader.read_exact(&mut chunk).unwrap();
            out.extend_from_slice(&chunk);
            let mut crlf = [0u8; 2];
            reader.read_exact(&mut crlf).unwrap();
        }
        String::from_utf8(out).unwrap()
    }

    fn read_chunk_size_line<R: Read>(reader: &mut R) -> usize {
        let mut line = Vec::new();
        let mut byte = [0u8; 1];
        loop {
            reader.read_exact(&mut byte).unwrap();
            if line.ends_with(b"\r\n") {
                break;
            }
            line.push(byte[0]);
        }
        let text = std::str::from_utf8(&line[..line.len() - 2]).unwrap();
        usize::from_str_radix(text.split(';').next().unwrap().trim(), 16).unwrap()
    }
}

use common::{correlation, relation};
use reqwest_free::{get, post};

fn full_request() -> Value {
    json!({
        "request_id": "http-smoke",
        "outer": relation(
            "outer_t",
            json!([{ "name": "k", "type": "Int64" }, { "name": "tag", "type": "Utf8" }]),
            json!([
                { "k": 1, "tag": "one" },
                { "k": null, "tag": "null-key" }
            ])
        ),
        "relations": [ relation(
            "inner_t",
            json!([{ "name": "ik", "type": "Int64" }, { "name": "v", "type": "Int64" }]),
            json!([ { "ik": 1, "v": 40 }, { "ik": null, "v": 5 } ])
        ) ],
        "subqueries": [
            { "alias": "has", "type": "exists", "input": "inner_t",
              "correlations": [ correlation("k", "ik") ] },
            { "alias": "total", "type": "scalar_agg", "input": "inner_t",
              "agg_func": "sum", "agg_argument": { "name": "v" },
              "correlations": [ correlation("k", "ik") ] }
        ]
    })
}

#[tokio::test]
async fn health_reports_version_and_catalog() {
    let (base, _g) = spawn_server().await;
    let resp = get(&base, "/healthz");
    assert_eq!(resp.status, 200);
    assert_eq!(resp.body["status"], json!("ok"));
    assert!(resp.body["service_version"].is_string());
    let cats = resp.body["catalog_relations"].as_array().unwrap();
    let names: Vec<&str> = cats.iter().map(|c| c.as_str().unwrap()).collect();
    assert!(names.contains(&"customers"));
    assert!(names.contains(&"empty_log"));
}

#[tokio::test]
async fn query_success_echoes_request_id_and_concrete_rows() {
    let (base, _g) = spawn_server().await;
    let resp = post(&base, "/v1/query", &full_request().to_string(), Some("rid-1234"));
    assert_eq!(resp.status, 200, "body: {}", resp.body);
    assert_eq!(resp.header_request_id.as_deref(), Some("rid-1234"));
    assert_eq!(resp.body["request_id"], json!("rid-1234"));
    assert_eq!(resp.body["status"], json!("ok"));
    assert_eq!(resp.body["results"]["equivalent"], json!(true));

    let rows = &resp.body["results"]["final_rows"]["rows"];
    assert_eq!(
        rows,
        &json!([
            { "k": 1, "tag": "one", "has": true, "total": 40 },
            { "k": null, "tag": "null-key", "has": false, "total": null }
        ])
    );
    // Both strategies report identical columns.
    assert_eq!(
        resp.body["results"]["naive"]["columns"],
        resp.body["results"]["decorrelated"]["columns"]
    );
}

#[tokio::test]
async fn generated_request_id_is_echoed_when_absent() {
    let (base, _g) = spawn_server().await;
    let resp = post(&base, "/v1/query", &full_request().to_string(), None);
    assert_eq!(resp.status, 200);
    let rid = resp.header_request_id.expect("generated request id header");
    assert!(rid.starts_with("req_"));
    assert_eq!(resp.body["request_id"], json!(rid));
}

#[tokio::test]
async fn not_in_is_rejected_over_http_with_category_and_step() {
    let (base, _g) = spawn_server().await;
    let mut body = full_request();
    body["subqueries"] = json!([
        { "alias": "bad", "type": "not_in", "input": "inner_t",
          "value": { "name": "v" },
          "correlations": [ correlation("k", "ik") ] }
    ]);
    let resp = post(&base, "/v1/query", &body.to_string(), Some("rid-notin"));
    assert_eq!(resp.status, 400);
    assert_eq!(resp.body["status"], json!("rejected"));
    assert_eq!(resp.body["error"]["category"], json!("UnsupportedNotIn"));
    assert_eq!(resp.body["error"]["code"], json!("not_in_unsupported"));
    assert_eq!(resp.body["error"]["at_step"], json!("validate"));
    assert_eq!(resp.body["request_id"], json!("rid-notin"));
}

#[tokio::test]
async fn multirow_scalar_returns_verified_error_envelope_with_both_traces() {
    let (base, _g) = spawn_server().await;
    let mut body = full_request();
    body["subqueries"] = json!([
        { "alias": "only_v", "type": "scalar", "input": "inner_t",
          "value": { "name": "v" },
          "correlations": [ correlation("k", "ik") ] }
    ]);
    // k=1 has exactly one inner row here → extend inner with a second match.
    body["relations"][0]["rows"] = json!([
        { "ik": 1, "v": 40 },
        { "ik": 1, "v": 41 },
        { "ik": null, "v": 5 }
    ]);
    let resp = post(&base, "/v1/query", &body.to_string(), Some("rid-card"));
    assert_eq!(resp.status, 422, "body: {}", resp.body);
    assert_eq!(resp.body["status"], json!("verified_error"));
    assert_eq!(resp.body["error"]["category"], json!("CardinalityViolation"));
    assert_eq!(resp.body["error"]["at_outer_row"], json!(0));
    // Both strategies' traces are present and both errored identically.
    assert_eq!(resp.body["results"]["equivalent"], json!(true));
    assert_eq!(resp.body["results"]["naive"]["status"], json!("error"));
    assert_eq!(resp.body["results"]["decorrelated"]["status"], json!("error"));
    assert_eq!(
        resp.body["results"]["naive"]["error"]["code"],
        resp.body["results"]["decorrelated"]["error"]["code"]
    );
}

#[tokio::test]
async fn malformed_json_is_a_validation_error() {
    let (base, _g) = spawn_server().await;
    let resp = post(&base, "/v1/query", "{ not json", Some("rid-bad"));
    assert_eq!(resp.status, 400);
    assert_eq!(resp.body["error"]["code"], json!("malformed_json"));
    assert_eq!(resp.body["error"]["at_step"], json!("http:parse"));
    assert_eq!(resp.body["request_id"], json!("rid-bad"));
}

#[tokio::test]
async fn response_contains_proof_steps_and_physical_pipeline() {
    let (base, _g) = spawn_server().await;
    let resp = post(&base, "/v1/query", &full_request().to_string(), Some("rid-proof"));
    assert_eq!(resp.status, 200);
    let lemmas: Vec<&str> = resp.body["rewrite"]["proof"]["steps"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["lemma"].as_str().unwrap())
        .collect();
    assert!(lemmas.contains(&"P4-empty-group"), "proof must cover COUNT/SUM empty-group distinction");
    let ops: Vec<&str> = resp.body["rewrite"]["subqueries"][1]["physical_steps"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["op"].as_str().unwrap())
        .collect();
    assert_eq!(
        ops,
        vec!["scan", "scan", "hash_build", "left_outer_hash_join", "group_aggregate", "scalar_project"]
    );
    // Uncertainties are reported in their own section.
    assert!(resp.body["uncertainties"].is_array());
}
