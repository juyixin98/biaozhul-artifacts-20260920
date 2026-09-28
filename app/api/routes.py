"""API 路由：规范登记、词法器构建、诊断查询、词法执行、运行日志重放。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.schemas import (
    BuildLexerRequest,
    BuildLexerResponse,
    CreateSpecResponse,
    DiagnosticsOut,
    LexerOut,
    LexerSpecIn,
    TokenizeRequest,
    TokenizeResponse,
)
from app.kernel.diagnostics import overlap_report, unreachable_report
from app.kernel.lexer import CompiledLexer
from app.kernel.nfa import ast_to_nfa, combine_nfas
from app.kernel.regex_ast import parse_pattern
from app.runlog import RunLogger, new_run_id
from app.spec.validation import to_rules, validate_spec

router = APIRouter()


def _logger(request: Request) -> RunLogger:
    repo = request.app.state.repo
    return RunLogger(new_run_id(), sink=repo.log_sink())


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/specs", response_model=CreateSpecResponse, status_code=201)
def create_spec(spec: LexerSpecIn, request: Request) -> CreateSpecResponse:
    repo = request.app.state.repo
    limits = request.app.state.settings.limits
    logger = _logger(request)
    validate_spec(spec, limits, logger)  # 语义校验：语法、空串、资源
    spec_id = repo.create_spec(spec.name, spec.version, spec.model_dump())
    logger.log(
        "api.specs",
        "spec_created",
        state={"spec_id": spec_id, "name": spec.name, "version": spec.version},
        rationale="规范通过校验并持久化（name+version 唯一索引）",
    )
    return CreateSpecResponse(spec_id=spec_id, run_id=logger.run_id)


@router.post("/lexers", response_model=BuildLexerResponse, status_code=201)
def build_lexer(body: BuildLexerRequest, request: Request) -> BuildLexerResponse:
    repo = request.app.state.repo
    limits = request.app.state.settings.limits
    logger = _logger(request)
    spec_row = repo.get_spec(body.spec_id)
    spec = LexerSpecIn(**spec_row["body"])
    rules = validate_spec(spec, limits, logger)

    lexer_id = repo.create_lexer(body.spec_id, status="building", model=None, stats=None)
    try:
        lexer = CompiledLexer.build(rules, limits, logger)
        nfas = [
            ast_to_nfa(parse_pattern(r.pattern, limits), tag=r.index, limits=limits)
            for r in rules
        ]
        overlaps = overlap_report(nfas, rules, limits, logger)
        unreachable = unreachable_report(nfas, rules, limits, logger)
    except Exception:
        repo.set_lexer_status(lexer_id, "failed")
        raise
    stats = {
        "rules": len(rules),
        "dfa_states": lexer.dfa.nstates,
        "overlapping_pairs": len(overlaps),
        "unreachable_rules": sum(1 for e in unreachable if e.unreachable),
    }
    repo.set_lexer_ready(lexer_id, lexer.to_dict(), stats)
    repo.add_diagnostics(lexer_id, "overlap", [e.to_dict() for e in overlaps])
    repo.add_diagnostics(lexer_id, "unreachable", [e.to_dict() for e in unreachable])
    logger.log(
        "api.lexers",
        "lexer_built",
        state={"lexer_id": lexer_id, **stats},
        rationale="编译成功，模型与诊断已持久化，状态置为 ready",
    )
    return BuildLexerResponse(
        lexer_id=lexer_id,
        run_id=logger.run_id,
        status="ready",
        stats=stats,
        diagnostics=DiagnosticsOut(
            overlaps=[e.to_dict() for e in overlaps],
            unreachable=[e.to_dict() for e in unreachable],
        ),
    )


@router.get("/lexers/{lexer_id}", response_model=LexerOut)
def get_lexer(lexer_id: int, request: Request) -> LexerOut:
    repo = request.app.state.repo
    row = repo.get_lexer(lexer_id)
    diag = repo.diagnostics_for(lexer_id)
    return LexerOut(
        lexer_id=lexer_id,
        spec_id=row["spec_id"],
        status=row["status"],
        stats=row["stats"],
        diagnostics=DiagnosticsOut(
            overlaps=diag.get("overlap", []),
            unreachable=diag.get("unreachable", []),
        ),
    )


@router.post("/lexers/{lexer_id}/tokenize", response_model=TokenizeResponse)
def tokenize(lexer_id: int, body: TokenizeRequest, request: Request) -> TokenizeResponse:
    repo = request.app.state.repo
    limits = request.app.state.settings.limits
    logger = _logger(request)
    row = repo.get_lexer(lexer_id)
    if row["status"] != "ready" or row["model"] is None:
        from app.errors import StateConflict

        raise StateConflict(
            code="LEXER_NOT_READY",
            message=f"词法器 id={lexer_id} 状态为 {row['status']!r}，不可执行词法分析",
            details={"lexer_id": lexer_id, "status": row["status"]},
        )
    lexer = CompiledLexer.from_dict(row["model"])
    tokens = lexer.tokenize(body.text, limits, logger)
    return TokenizeResponse(run_id=logger.run_id, tokens=[t.to_dict() for t in tokens])


@router.get("/runs/{run_id}/logs")
def run_logs(run_id: str, request: Request) -> dict:
    repo = request.app.state.repo
    return {"run_id": run_id, "entries": repo.logs_for_run(run_id)}
