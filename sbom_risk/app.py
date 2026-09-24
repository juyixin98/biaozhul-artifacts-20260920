"""FastAPI 应用：HTTP 接口层。

环境变量：
- SBOM_FEED_PATH       漏洞馈送 JSON 路径（默认 fixtures/vuln-feed.json）
- SBOM_PUBLIC_KEY_PATH Ed25519 公钥 PEM 路径（默认 fixtures/keys/test_feed_public.pem）
- SBOM_ALLOW_UNSIGNED  设为 "1"/"true" 时允许无公钥加载未签名馈送（仅限调试）
- SBOM_HOST / SBOM_PORT 仅被 scripts/run_dev.sh 使用
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .feed import FeedError, LoadedFeed, load_feed
from .graph import GraphError
from .matching import analyze_sbom
from .models import SBOM

_REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings:
    def __init__(self) -> None:
        self.feed_path = Path(
            os.environ.get("SBOM_FEED_PATH", str(_REPO_ROOT / "fixtures" / "vuln-feed.json"))
        )
        pk_env = os.environ.get("SBOM_PUBLIC_KEY_PATH")
        self.public_key_path = Path(pk_env) if pk_env else _REPO_ROOT / "fixtures" / "keys" / "test_feed_public.pem"
        self.allow_unsigned = os.environ.get("SBOM_ALLOW_UNSIGNED", "").lower() in ("1", "true", "yes")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            app.state.feed = load_feed(
                settings.feed_path,
                settings.public_key_path if settings.public_key_path.exists() else None,
                allow_unsigned=settings.allow_unsigned,
            )
        except FeedError as exc:
            # 签名/夹具问题必须让服务显式启动失败，而不是静默无情报运行
            raise RuntimeError(f"启动失败：{exc}") from exc
        app.state.settings = settings
        yield

    app = FastAPI(
        title="SBOM 风险匹配服务",
        version=__version__,
        description="自定义 JSON SBOM + SemVer 区间 + 本地签名漏洞夹具的纯后端风险匹配。",
        lifespan=lifespan,
    )

    def get_feed(request: Request) -> LoadedFeed:
        return request.app.state.feed

    @app.exception_handler(GraphError)
    async def graph_error_handler(request: Request, exc: GraphError):
        return JSONResponse(status_code=422, content={"error": "invalid_sbom_graph", "detail": str(exc)})

    @app.get("/healthz", tags=["meta"])
    async def healthz(feed: LoadedFeed = Depends(get_feed)):
        return {"status": "ok", "service_version": __version__, "feed_verified": feed.verified}

    @app.get("/api/v1/feed/info", tags=["feed"])
    async def feed_info(feed: LoadedFeed = Depends(get_feed)):
        return {
            "feed_version": feed.payload.feed_version,
            "generated_at": feed.payload.generated_at,
            "verified": feed.verified,
            "kid": feed.kid,
            "vulnerability_count": len(feed.payload.vulnerabilities),
            "vulnerabilities": [
                {
                    "id": v.id,
                    "ecosystem": v.ecosystem,
                    "name": v.name,
                    "ranges": v.ranges,
                    "severity": v.severity,
                }
                for v in feed.payload.vulnerabilities
            ],
        }

    @app.post("/api/v1/match", tags=["match"])
    async def match_sbom(sbom: SBOM, feed: LoadedFeed = Depends(get_feed)):
        """提交 SBOM，返回受影响/未知状态、依赖路径与依赖环。"""
        return analyze_sbom(sbom, feed)

    return app


app = create_app()
