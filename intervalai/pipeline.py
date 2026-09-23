"""High-level pipeline tying lexer/parser/resolver/IR/analysis together."""

from . import analyzer as analyzer_mod
from . import concrete
from . import ir as ir_mod
from . import parser
from .abstract_env import Env
from .resolve import resolve


class AnalyzeOutput:
    def __init__(self, prog, cfg, result, input_bounds):
        self.prog = prog
        self.cfg = cfg
        self.result = result
        self.input_bounds = input_bounds

    def to_dict(self, include_points=True, include_cfg=False):
        r = self.result
        exit_env = r.exit_states.get(self.cfg.exit)
        d = {
            "status": "ok",
            "loop_headers": r.headers,
            "ascending_iterations": r.iterations,
            "narrow_rounds": r.narrow_rounds,
            "alarms": r.alarms,
            "summary": _summary(r.alarms),
            "exit_state": None if exit_env is None else
                         self._public(exit_env).to_dict(),
            "entry_states": {
                str(bid): self._public(env).to_dict()
                for bid, env in sorted(r.entry_states.items())
            },
        }
        if include_points:
            d["points"] = r.points
        if include_cfg:
            d["cfg"] = dump_cfg(self.cfg)
        return d

    def _public(self, env):
        names = {d.name for d in self.prog.var_decls}
        if env.is_bottom():
            return env
        keep = {k: v for k, v in env.vars.items() if k in names}
        return Env(keep, dict(env.arrays))


def _summary(alarms):
    kinds = {}
    for a in alarms:
        key = (a["kind"], a.get("severity", "possible"))
        kinds[key] = kinds.get(key, 0) + 1
    return [{"kind": k[0], "severity": k[1], "count": c}
            for k, c in sorted(kinds.items())]


def dump_cfg(cfg):
    blocks = []
    for b in cfg.blocks:
        term = b.term
        td = {"type": type(term).__name__}
        if isinstance(term, ir_mod.Jump):
            td["target"] = term.target
        elif isinstance(term, ir_mod.Branch):
            td["yes"] = term.yes
            td["no"] = term.no
        blocks.append({
            "id": b.id,
            "is_loop_header": b.is_header,
            "loc": b.loc.to_dict() if b.loc else None,
            "instructions": [
                {"type": type(i).__name__,
                 "loc": i.loc.to_dict() if getattr(i, "loc", None) else None,
                 **{k: v for k, v in vars(i).items()
                    if k not in ("loc",)}}
                for i in b.instrs
            ],
            "terminator": td,
        })
    return {
        "entry": cfg.entry,
        "exit": cfg.exit,
        "temps": cfg.temps,
        "blocks": blocks,
        "edges": [{"src": u, "dst": v}
                  for u, vs in sorted(cfg.succs.items()) for v in vs],
        "reverse_post_order": cfg.rpo,
    }


def parse_program(src):
    return resolve(parser.parse(src))


def analyze_source(src, input_bounds=None, include_points=True, include_cfg=False):
    prog = parse_program(src)
    cfg = ir_mod.lower(prog)
    scalars = {d.name: d for d in prog.var_decls}
    env0 = Env.initial(scalars, prog.arr_decls, input_bounds)
    result = analyzer_mod.analyze_cfg(cfg, env0)
    return AnalyzeOutput(prog, cfg, result, input_bounds or {}) \
        .to_dict(include_points=include_points, include_cfg=include_cfg)


def execute_source(src, inputs=None, step_limit=concrete.DEFAULT_STEP_LIMIT):
    prog = parse_program(src)
    return concrete.execute(prog, inputs, step_limit)
