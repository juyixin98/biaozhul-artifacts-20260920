"""应用服务层：解析 SBOM -> 核对 -> 汇总 -> 持久化。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from . import db as dbmod
from .engine import Vulnerability, analyze, load_fixture
from .sbom import SbomError, parse_cyclonedx

DEFAULT_FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "vulnerabilities.json"
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "sbom.db"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _flatten(analysis: dict) -> list[dict]:
    flat: list[dict] = []
    for item in analysis["affected"]:
        c = item["component"]
        flat.append({
            "component_key": c["purl"], "purl": c["purl"],
            "ecosystem": c["ecosystem"], "name": c["name"],
            "version": c["version"], "scope": c["scope"],
            "disposition": "affected",
            "is_transitive": bool(item["is_transitive"]),
            "vulnerability_ids": [m["vulnerability_id"] for m in item["matched"]],
        })
    for item in analysis["unknown"]:
        c = item["component"]
        flat.append({
            "component_key": c["purl"], "purl": c["purl"],
            "ecosystem": c["ecosystem"], "name": c["name"],
            "version": c["version"], "scope": c["scope"],
            "disposition": "unknown", "is_transitive": False,
            "vulnerability_ids": [m["vulnerability_id"] for m in item["matched"]
                                  if m.get("vulnerability_id")],
        })
    for item in analysis["not_affected"]:
        c = item["component"]
        flat.append({
            "component_key": c["purl"], "purl": c["purl"],
            "ecosystem": c["ecosystem"], "name": c["name"],
            "version": c["version"], "scope": c["scope"],
            "disposition": "not_affected", "is_transitive": False,
            "vulnerability_ids": [],
        })
    return flat


def _graph(report) -> dict:
    return {
        "node_count": len(report.components),
        "edge_count": len(report.edges),
        "roots": [report._by_key[k].purl.canonical() for k in report.root_keys],
        "edges": [
            {"from": report._by_key[a].purl.canonical() if a in report._by_key else a,
             "to": report._by_key[z].purl.canonical() if z in report._by_key else z}
            for a, z in report.edges
        ],
        "cycles": [
            [report._by_key[k].purl.canonical() if k in report._by_key else k
             for k in cyc]
            for cyc in report.cycles
        ],
    }


def run_check(doc: dict, vulns: list[Vulnerability], fixture_meta: dict,
              conn: sqlite3.Connection, raw_bytes: bytes) -> dict:
    report = parse_cyclonedx(doc)
    analysis = analyze(report, vulns)

    result = {
        "request": doc,
        "sbom": {
            "spec_version": report.spec_version,
            "serial_number": report.serial_number,
            "bom_version": report.version,
        },
        "summary": {
            "total_components": len(report.components),
            "affected_components": len(analysis["affected"]),
            "unknown_components": len(analysis["unknown"]),
            "not_affected_components": len(analysis["not_affected"]),
            "affected_findings": sum(len(a["matched"]) for a in analysis["affected"]),
            "cycles_detected": len(report.cycles),
            "duplicates_merged": sum(
                len(c.merged_from) for c in report.components),
            "ignored_components": len(report.ignored),
        },
        "results": analysis,
        "dependency_graph": _graph(report),
        "ignored_components": [
            {"bom_ref": i.ref, "name": i.name, "reason": i.reason, "detail": i.detail}
            for i in report.ignored
        ],
        "warnings": report.warnings,
        "data_source": {
            "type": "local_fixture",
            "generated_at": fixture_meta.get("generated_at"),
            "not_real_advisory": bool(fixture_meta.get("not_real_advisory", False)),
            "notice": ("漏洞数据来自随附的本地合成夹具，仅用于离线核对演示与测试；"
                       "它不是实时漏洞情报源，不覆盖最新漏洞。"),
            "vulnerability_count": len(vulns),
        },
    }

    run_id = uuid.uuid4().hex
    created_at = dbmod.utc_now_iso()
    source_sha256 = sha256_bytes(raw_bytes)
    result["run"] = {
        "id": run_id,
        "created_at": created_at,
        "source_sha256": source_sha256,
    }

    dbmod.save_run(
        conn, run_id=run_id, created_at=created_at,
        report_meta={"spec_version": report.spec_version,
                     "serial_number": report.serial_number,
                     "bom_version": report.version},
        source_sha256=source_sha256,
        component_count=len(report.components),
        result=result, flat_components=_flatten(analysis))
    return result
