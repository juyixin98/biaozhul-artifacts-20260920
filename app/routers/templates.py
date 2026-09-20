"""Template catalog endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import catalog
from ..db import get_db
from ..schemas import (
    TemplateCreate,
    TemplateOut,
    TemplateVersionCreate,
    VersionOut,
)

router = APIRouter(prefix="/templates", tags=["templates"])


def _serialize(template) -> dict:
    data = TemplateOut(
        id=template.id,
        key=template.key,
        name=template.name,
        current_version=template.current_version,
        created_at=template.created_at,
        versions=[
            VersionOut(
                version=v.version,
                status=v.status,
                created_at=v.created_at,
                published_at=v.published_at,
            )
            for v in sorted(template.versions, key=lambda v: v.version)
        ],
    )
    return data.model_dump(mode="json")


@router.post("", status_code=201)
def create_template(body: TemplateCreate, db: Session = Depends(get_db)):
    template = catalog.create_template(db, body)
    return _serialize(template)


@router.get("")
def list_templates(db: Session = Depends(get_db)):
    return {"templates": [_serialize(t) for t in catalog.list_templates(db)]}


@router.get("/{key}")
def get_template(key: str, db: Session = Depends(get_db)):
    return _serialize(catalog.get_template_or_404(db, key))


@router.get("/{key}/versions/{version}")
def get_version(key: str, version: int, db: Session = Depends(get_db)):
    template = catalog.get_template_or_404(db, key)
    v = catalog._get_version(db, template.id, version)
    return {
        "key": key,
        "version": v.version,
        "status": v.status,
        "definition": v.definition,
        "created_at": v.created_at,
        "published_at": v.published_at,
    }


@router.post("/{key}/versions", status_code=201)
def create_version(key: str, body: TemplateVersionCreate, db: Session = Depends(get_db)):
    v = catalog.create_version(db, key, body)
    return {"key": key, "version": v.version, "status": v.status}


@router.post("/{key}/versions/{version}/publish")
def publish_version(key: str, version: int, db: Session = Depends(get_db)):
    v = catalog.publish_version(db, key, version)
    return {"key": key, "version": v.version, "status": v.status}


@router.post("/{key}/rollback/{version}")
def rollback(key: str, version: int, db: Session = Depends(get_db)):
    """Point 'current' at an older published version (immutable repoint)."""
    v = catalog.rollback(db, key, version)
    return {"key": key, "current_version": v.version, "status": v.status}
