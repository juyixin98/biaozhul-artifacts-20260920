from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import PolicyVersion, Purpose, User, utcnow
from ..schemas import PolicyVersionIn, PurposeIn
from ..security import get_current_user, require_admin

router = APIRouter(tags=["policies"])


@router.post("/purposes", status_code=201)
def create_purpose(body: PurposeIn, db: Session = Depends(get_db),
                   user: User = Depends(require_admin)):
    existing = db.scalar(select(Purpose).where(Purpose.org_id == user.org_id,
                                               Purpose.code == body.code))
    if existing:
        raise HTTPException(status_code=409, detail="purpose code already exists")
    purpose = Purpose(org_id=user.org_id, code=body.code, name=body.name)
    db.add(purpose)
    db.commit()
    return {"id": str(purpose.id), "code": purpose.code, "name": purpose.name}


@router.get("/purposes")
def list_purposes(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = db.scalars(select(Purpose).where(Purpose.org_id == user.org_id)).all()
    return {"purposes": [{"id": str(p.id), "code": p.code, "name": p.name} for p in rows]}


@router.post("/purposes/{code}/policy-versions", status_code=201)
def create_policy_version(code: str, body: PolicyVersionIn,
                          db: Session = Depends(get_db),
                          user: User = Depends(require_admin)):
    purpose = db.scalar(select(Purpose).where(Purpose.org_id == user.org_id,
                                              Purpose.code == code))
    if purpose is None:
        raise HTTPException(status_code=404, detail="purpose not found")
    existing = db.scalar(select(PolicyVersion).where(
        PolicyVersion.purpose_id == purpose.id, PolicyVersion.version == body.version))
    if existing:
        raise HTTPException(status_code=409, detail="version already exists")
    pv = PolicyVersion(org_id=user.org_id, purpose_id=purpose.id,
                       version=body.version, content=body.content)
    db.add(pv)
    db.commit()
    return {"id": str(pv.id), "version": pv.version, "status": pv.status}


@router.get("/purposes/{code}/policy-versions")
def list_policy_versions(code: str, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    purpose = db.scalar(select(Purpose).where(Purpose.org_id == user.org_id,
                                              Purpose.code == code))
    if purpose is None:
        raise HTTPException(status_code=404, detail="purpose not found")
    rows = db.scalars(select(PolicyVersion)
                      .where(PolicyVersion.purpose_id == purpose.id)
                      .order_by(PolicyVersion.version)).all()
    return {"versions": [
        {"id": str(pv.id), "version": pv.version, "status": pv.status,
         "content": pv.content,
         "published_at": pv.published_at.isoformat() if pv.published_at else None}
        for pv in rows
    ]}


@router.post("/policy-versions/{version_id}/publish")
def publish_policy_version(version_id: str, db: Session = Depends(get_db),
                           user: User = Depends(require_admin)):
    pv = db.get(PolicyVersion, version_id)
    if pv is None or pv.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="policy version not found")
    if pv.status == "published":
        raise HTTPException(status_code=409, detail="already published")
    pv.status = "published"
    pv.published_at = utcnow()
    db.commit()
    return {"id": str(pv.id), "version": pv.version, "status": pv.status}


# 政策版本发布后不可修改：不提供更新/删除接口。
@router.api_route("/policy-versions/{version_id}", methods=["PUT", "PATCH", "DELETE"])
def immutable_policy_version(version_id: str):
    raise HTTPException(status_code=405, detail="policy versions are immutable")
