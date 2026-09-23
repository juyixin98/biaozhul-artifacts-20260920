//! 集成测试公共辅助。

#![allow(dead_code)]

use gas_meter::{execute, ExecRequest, HostState, MeteringVersion, Receipt};
use std::sync::Arc;

pub fn engines() -> Arc<gas_meter::Engines> {
    Arc::new(gas_meter::Engines::new().expect("build engines"))
}

pub fn compile_wat(wat: &str) -> Vec<u8> {
    wat::parse_str(wat).expect("wat must compile")
}

pub struct Harness {
    pub engines: Arc<gas_meter::Engines>,
    pub host: Arc<HostState>,
}

impl Harness {
    pub fn new() -> Self {
        Self {
            engines: engines(),
            host: Arc::new(HostState::new()),
        }
    }

    pub fn run(&self, wasm: Vec<u8>, input: Vec<u8>, version: MeteringVersion) -> Receipt {
        self.run_req(ExecRequest::new(wasm, input, version))
    }

    pub fn run_req(&self, req: ExecRequest) -> Receipt {
        execute(&self.engines, &self.host, req).expect("execute returns receipt")
    }

    pub fn sample(&self, id: &str, version: MeteringVersion, input: &[u8]) -> Receipt {
        let s = gas_meter::samples::SAMPLES
            .iter()
            .find(|s| s.id == id)
            .unwrap_or_else(|| panic!("sample {id}"));
        let wasm = gas_meter::samples::compile(s).unwrap();
        self.run(wasm, input.to_vec(), version)
    }

    pub fn state_keys(&self) -> Vec<String> {
        self.host.current().keys().cloned().collect()
    }
}

pub fn receipt_output(r: &Receipt) -> Vec<u8> {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD
        .decode(&r.output_base64)
        .unwrap()
}
