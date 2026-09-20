"""Template catalog: create, version, publish and roll back templates.

Versioning model
----------------
* Versions start as ``draft`` and become ``published`` exactly once.
* A published row is never mutated again — neither its definition nor status.
* ``Template.current_version`` is a pointer. A new publish advances it; a
  roll back repoints it at an already published version. Both operations only
  affect instances created afterwards; running instances keep their snapshot.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import DomainError
from .models import Template, TemplateVersion, utcnow
from .schemas import Definition, TemplateCreate, TemplateVersionCreate
from .validator import validate_definition


def _definition_to_dict(definition: Definition) -> dict:
    return definition.model_dump(mode="json")


def create_template(db: Session, body: TemplateCreate) -> Template:
    issues = validate_definition(body.definition)
    if issues:
        raise DomainError("invalid_definition", "definition failed validation", 400, issues)

    existing = db.scalar(select(Template).where(Template.key == body.key))
    if existing is not None:
        raise DomainError("template_key_exists", f"template key {body.key!r} already exists", 409)

    template = Template(key=body.key, name=body.name)
    db.add(template)
    db.flush()
    version = TemplateVersion(
        template_id=template.id,
        version=1,
        status="draft",
        definition=_definition_to_dict(body.definition),
    )
    db.add(version)
    db.commit()
    db.refresh(template)
    return template


def create_version(db, template_key: str, body: TemplateVersionCreate) -> TemplateVersion:
    template = _get_template_or_404(db, template_key)
    issues = validate_definition(body.definition)
    if issues:
        raise DomainError("invalid_definition", "definition failed validation", 400, issues)

    draft = db.scalar(
        select(TemplateVersion).where(
            TemplateVersion.template_id == template.id, TemplateVersion.status == "draft"
        )
    )
    if draft is not None:
        raise DomainError("draft_exists", "an unpublished draft version already exists", 409)

    next_number = (template.current_version or 0) + 1
    version = TemplateVersion(
        template_id=template.id,
        version=next_number,
        status="draft",
        definition=_definition_to_dict(body.definition),
    )
    db.add(version)
    db.commit()
    db.refresh(version)
    return version


def publish_version(db, template_key: str, version_number: int) -> TemplateVersion:
    template = _get_template_or_404(db, template_key)
    version = _get_version(db, template.id, version_number)
    if version.status == "published":
        raise DomainError("already_published", f"version {version_number} is already published", 409)

    # Re-validate defensively (the draft content never changed, but this keeps
    # the invariant that every published definition passes validation).
    issues = validate_definition(Definition.model_validate(version.definition))
    if issues:
        raise DomainError("invalid_definition", "definition failed validation", 400, issues)

    version.status = "published"
    version.published_at = utcnow()
    template.current_version = version.version
    db.commit()
    db.refresh(version)
    return version


def rollback(db, template_key: str, target_version: int) -> TemplateVersion:
    """Point the template at an older published version.

    No version is modified or deleted; the target keeps its number. Instances
    already bound to a newer version continue on that snapshot.
    """
    template = _get_template_or_404(db, template_key)
    version = _get_version(db, template.id, target_version)
    if version.status != "published":
        raise DomainError("version_not_published", "can only roll back to a published version", 409)

    template.current_version = version.version
    db.commit()
    db.refresh(version)
    return version


def get_template_or_404(db: Session, template_key: str) -> Template:
    return _get_template_or_404(db, template_key)


def get_published_version(db: Session, template_key: str, version_number: int | None) -> tuple[Template, TemplateVersion]:
    template = _get_template_or_404(db, template_key)
    if version_number is None:
        if template.current_version is None:
            raise DomainError("template_not_published", "template has no published version", 409)
        version_number = template.current_version
    version = _get_version(db, template.id, version_number)
    if version.status != "published":
        raise DomainError("version_not_published", f"version {version_number} is not published", 409)
    return template, version


def list_templates(db: Session) -> list[Template]:
    return list(db.scalars(select(Template).order_by(Template.created_at, Template.key)).unique())


def _get_template_or_404(db: Session, template_key: str) -> Template:
    template = db.scalar(select(Template).where(Template.key == template_key))
    if template is None:
        raise DomainError("template_not_found", f"template {template_key!r} not found", 404)
    return template


def _get_version(db: Session, template_id, version_number: int) -> TemplateVersion:
    version = db.scalar(
        select(TemplateVersion).where(
            TemplateVersion.template_id == template_id, TemplateVersion.version == version_number
        )
    )
    if version is None:
        raise DomainError("version_not_found", f"version {version_number} not found", 404)
    return version
