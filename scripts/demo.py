"""端到端演示：授予 -> 验证 -> 幂等重放 -> 撤回 -> 政策升级 -> 导出 -> 删除 -> 重建。

用法: python -m scripts.demo   (需要服务已启动，默认 http://localhost:8010)
"""

import os
import uuid

import requests

BASE = os.getenv("API_BASE", "http://localhost:8010")
ADMIN = {"Authorization": "Bearer acme-admin-token"}
AUDITOR = {"Authorization": "Bearer acme-auditor-token"}


def show(step: str, resp: requests.Response) -> dict:
    print(f"\n== {step} -> {resp.status_code}")
    body = resp.json() if resp.content else {}
    print(body)
    resp.raise_for_status()
    return body


def main() -> None:
    run = uuid.uuid4().hex[:8]
    subject = f"user-{run}@example.com"

    pv = show("list policy versions",
              requests.get(f"{BASE}/purposes/marketing/policy-versions", headers=ADMIN))
    pv1_id = pv["versions"][0]["id"]

    grant = show("grant consent", requests.post(f"{BASE}/consents/events", headers=ADMIN, json={
        "event_id": f"evt-{run}-1", "event_type": "grant", "subject_ref": subject,
        "purpose_code": "marketing", "policy_version_id": pv1_id, "expected_version": 0,
    }))

    show("verify (granted)", requests.get(
        f"{BASE}/consents/verify", headers=AUDITOR,
        params={"subject_ref": subject, "purpose_code": "marketing"}))

    show("duplicate grant (idempotent replay)", requests.post(
        f"{BASE}/consents/events", headers=ADMIN, json={
            "event_id": f"evt-{run}-1", "event_type": "grant", "subject_ref": subject,
            "purpose_code": "marketing", "policy_version_id": pv1_id, "expected_version": 0,
        }))

    show("withdraw", requests.post(f"{BASE}/consents/events", headers=ADMIN, json={
        "event_id": f"evt-{run}-2", "event_type": "withdraw", "subject_ref": subject,
        "purpose_code": "marketing", "expected_version": grant["resulting_version"],
    }))

    show("verify (withdrawn)", requests.get(
        f"{BASE}/consents/verify", headers=AUDITOR,
        params={"subject_ref": subject, "purpose_code": "marketing"}))

    r = requests.post(f"{BASE}/purposes/marketing/policy-versions", headers=ADMIN,
                      json={"version": 2, "content": "Marketing communications policy v2"})
    if r.status_code == 409:  # 重复运行 demo：v2 已存在，直接复用
        versions = requests.get(f"{BASE}/purposes/marketing/policy-versions",
                                headers=ADMIN).json()["versions"]
        pv2 = next(v for v in versions if v["version"] == 2)
        print("\n== create policy v2 -> already exists, reuse")
    else:
        pv2 = show("create policy v2", r)
    if pv2["status"] != "published":
        show("publish policy v2", requests.post(
            f"{BASE}/policy-versions/{pv2['id']}/publish", headers=ADMIN))

    show("re-grant on v2", requests.post(f"{BASE}/consents/events", headers=ADMIN, json={
        "event_id": f"evt-{run}-3", "event_type": "grant", "subject_ref": subject,
        "purpose_code": "marketing", "policy_version_id": pv2["id"], "expected_version": 2,
    }))

    show("export subject data", requests.post(
        f"{BASE}/subjects/{subject}/exports", headers=ADMIN))

    show("rebuild state from history", requests.post(f"{BASE}/consents/rebuild", headers=ADMIN))

    show("delete subject (erasure)", requests.delete(f"{BASE}/subjects/{subject}", headers=ADMIN))

    resp = requests.get(f"{BASE}/consents/verify", headers=AUDITOR,
                        params={"subject_ref": subject, "purpose_code": "marketing"})
    print(f"\n== verify after deletion -> {resp.status_code} (expected 404)")

    show("audit log (no personal fields)", requests.get(f"{BASE}/audit-log", headers=AUDITOR))


if __name__ == "__main__":
    main()
