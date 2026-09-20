import json
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.engine import publish_template
from app.models import TemplateVersion
from app.serializers import template_out
from app.schemas import TemplateVersionOut

router = APIRouter(tags=["demo"])

DEMO_DIR = Path(__file__).resolve().parent.parent.parent / "demo"


@router.post("/demo/setup", response_model=list[TemplateVersionOut], status_code=201)
async def setup_demo(session: AsyncSession = Depends(get_session)):
    """Publish the demo templates if they do not exist yet (idempotent)."""
    created = []
    for path in sorted(DEMO_DIR.glob("*.json")):
        code = path.stem
        definition = json.loads(path.read_text(encoding="utf-8"))
        exists = await session.scalar(
            select(TemplateVersion.version)
            .where(TemplateVersion.template_code == code)
            .limit(1)
        )
        if exists is not None:
            continue
        record = await publish_template(
            session,
            template_code=code,
            name=code,
            definition=definition,
            actor="demo",
        )
        created.append(record)
    await session.commit()
    return [template_out(r) for r in created]


@router.get("/health")
async def health():
    return {"status": "ok"}
