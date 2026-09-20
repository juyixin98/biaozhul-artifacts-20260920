"""初始化演示数据：两个组织、管理员/只读审计员、目的与已发布政策版本。

用法: python -m scripts.seed   (幂等，可重复执行)
"""

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Organization, PolicyVersion, Purpose, User, utcnow

SEED = {
    "acme": {
        "users": [("admin@acme.example", "admin", "acme-admin-token"),
                  ("auditor@acme.example", "auditor", "acme-auditor-token")],
        "purposes": [("marketing", "Marketing communications"),
                     ("analytics", "Product analytics")],
    },
    "globex": {
        "users": [("admin@globex.example", "admin", "globex-admin-token")],
        "purposes": [("marketing", "Marketing communications")],
    },
}


def main() -> None:
    db = SessionLocal()
    try:
        for org_name, cfg in SEED.items():
            org = db.scalar(select(Organization).where(Organization.name == org_name))
            if org is None:
                org = Organization(name=org_name)
                db.add(org)
                db.flush()

            for email, role, token in cfg["users"]:
                if not db.scalar(select(User).where(User.token == token)):
                    db.add(User(org_id=org.id, email=email, role=role, token=token))

            for code, name in cfg["purposes"]:
                purpose = db.scalar(select(Purpose).where(
                    Purpose.org_id == org.id, Purpose.code == code))
                if purpose is None:
                    purpose = Purpose(org_id=org.id, code=code, name=name)
                    db.add(purpose)
                    db.flush()
                if not db.scalar(select(PolicyVersion).where(
                        PolicyVersion.purpose_id == purpose.id,
                        PolicyVersion.version == 1)):
                    db.add(PolicyVersion(
                        org_id=org.id, purpose_id=purpose.id, version=1,
                        content=f"{name} policy v1", status="published",
                        published_at=utcnow()))
        db.commit()
        print("seeded. tokens:")
        for org_name, cfg in SEED.items():
            for email, role, token in cfg["users"]:
                print(f"  {org_name:8s} {role:8s} {token}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
