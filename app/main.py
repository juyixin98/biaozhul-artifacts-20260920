"""FastAPI 入口：HTTP 接口定义。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI

from .crypto import canonical_json, sha256_hex, sign_report
from .matcher import match_sbom
from .models import MatchRequest, Vulnerability

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "vulnerabilities.json"

app = FastAPI(
    title="sbom-risk-matcher",
    version="0.1.0",
    description="SBOM 风险匹配服务：自定义 JSON SBOM + 有界 SemVer 范围语法的漏洞匹配",
)


def load_fixture_vulnerabilities() -> list[Vulnerability]:
    """加载服务内置的本地漏洞夹具。"""
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return [Vulnerability.model_validate(item) for item in raw]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/v1/vulnerabilities")
def list_vulnerabilities() -> dict:
    """返回当前内置漏洞夹具内容及其摘要。"""
    vulns = load_fixture_vulnerabilities()
    return {
        "count": len(vulns),
        "digest": "sha256:" + sha256_hex(canonical_json([v.model_dump() for v in vulns])),
        "vulnerabilities": vulns,
    }


@app.post("/v1/match")
def match(request: MatchRequest) -> dict:
    """对 SBOM 执行漏洞匹配。

    请求体未提供 vulnerabilities 时使用内置夹具。
    响应包含 SBOM 摘要与整份报告的 HMAC-SHA256 签名。
    """
    vulnerabilities = (
        request.vulnerabilities
        if request.vulnerabilities is not None
        else load_fixture_vulnerabilities()
    )
    report = match_sbom(request.sbom, vulnerabilities)
    report["sbom_digest"] = "sha256:" + sha256_hex(canonical_json(request.sbom.model_dump()))
    report["signature"] = "hmac-sha256:" + sign_report(report)
    return report
