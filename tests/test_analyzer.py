"""核心分析器验收测试。

覆盖题目要求的关键维度：
  - 入口污点（source）、赋值、运算传播、清洗（clean）、汇（sink）
  - 不同调用上下文（同一函数干净/污点实参的区分）
  - 线性递归与相互递归
  - 条件清洗（确定 vs 已知过近似误报）
  - while 循环的 0..n 次近似
  - 未定义函数的保守传播（FP-1）
  - 路径轨迹的连贯性与源码位置
"""

import unittest

from taintflow.service import analyze_source


def run(src, **cfg):
    from taintflow.config import AnalysisConfig
    try:
        config = AnalysisConfig.from_dict(cfg or None)
    except ValueError as e:
        return {"ok": False, "error": {"type": "ConfigError", "message": str(e)}}
    return analyze_source(src, config)


def classifications(result):
    return [a["classification"] for a in result["alerts"]]


def sink_lines(result):
    return [a["sink"]["line"] for a in result["alerts"]]


class TestDirectFlows(unittest.TestCase):
    def test_direct_source_to_sink(self):
        r = run("fn main(){ x = source(); sink(x); }")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["alerts"]), 1)
        self.assertEqual(r["alerts"][0]["classification"], "true_positive")

    def test_no_source_no_alert(self):
        r = run("fn main(){ x = 42; sink(x); }")
        self.assertEqual(len(r["alerts"]), 0)

    def test_sink_with_string_literal(self):
        r = run('fn main(){ sink("constant"); }')
        self.assertEqual(len(r["alerts"]), 0)

    def test_assignment_chain(self):
        r = run("fn main(){ a=source(); b=a; c=b; sink(c); }")
        self.assertEqual(len(r["alerts"]), 1)

    def test_arithmetic_propagates(self):
        r = run("fn main(){ a=source(); b=a+1; c=b*2; sink(c); }")
        self.assertEqual(len(r["alerts"]), 1)

    def test_unary_propagates(self):
        r = run("fn main(){ a=source(); b=-a; sink(b); }")
        self.assertEqual(len(r["alerts"]), 1)

    def test_comparison_then_use(self):
        r = run("fn main(){ a=source(); b = a < 10; sink(b); }")
        self.assertEqual(len(r["alerts"]), 1)


class TestSanitizer(unittest.TestCase):
    def test_sanitize_blocks(self):
        r = run("fn main(){ a=source(); b=clean(a); sink(b); }")
        self.assertEqual(len(r["alerts"]), 0)

    def test_sanitize_inline(self):
        r = run("fn main(){ sink(clean(source())); }")
        self.assertEqual(len(r["alerts"]), 0)

    def test_sanitize_does_not_clean_alias(self):
        # clean 返回新值，旧别名仍脏
        r = run("fn main(){ a=source(); b=clean(a); sink(a); }")
        self.assertEqual(len(r["alerts"]), 1)

    def test_sanitize_one_branch_keeps_other(self):
        r = run("""
        fn main(){
            a = source();
            if (c) { b = clean(a); } else { b = a; }
            sink(b);
        }""")
        self.assertEqual(len(r["alerts"]), 1)
        self.assertIn("possible_false_positive", r["alerts"][0]["classification"])


class TestContextSensitivity(unittest.TestCase):
    def test_same_function_clean_and_tainted_args(self):
        r = run("""
        fn passthrough(x){ return x; }
        fn main(){
            t = source();
            sink(passthrough(t));   // 报
            c = 42;
            sink(passthrough(c));   // 不报
        }""")
        self.assertEqual(len(r["alerts"]), 1)
        self.assertEqual(r["alerts"][0]["classification"], "true_positive")

    def test_context_through_two_levels(self):
        r = run("""
        fn inner(x){ return x; }
        fn outer(x){ return inner(x); }
        fn main(){
            sink(outer(source()));   // 报
            sink(outer(7));          // 不报
        }""")
        self.assertEqual(len(r["alerts"]), 1)

    def test_polyvariant_different_origins(self):
        # 两次调用的实参来自不同 source：产生两条独立告警
        r = run("""
        fn p(x){ return x; }
        fn main(){
            a = source();
            b = source();
            sink(p(a));
            sink(p(b));
        }""")
        self.assertEqual(len(r["alerts"]), 2)

    def test_source_inside_callee_reaches_sink_in_caller(self):
        r = run("""
        fn producer(){ x = source(); return x; }
        fn main(){ sink(producer()); }
        """)
        self.assertEqual(len(r["alerts"]), 1)
        a = r["alerts"][0]
        self.assertEqual(a["source_point"]["function"], "producer")
        self.assertEqual(a["sink"]["function"], "main")


class TestRecursion(unittest.TestCase):
    def test_linear_recursion_propagates(self):
        r = run("""
        fn walk(n, acc){
            if (n == 0) { return acc; } else { return walk(n-1, acc); }
        }
        fn main(){ t=source(); sink(walk(10,t)); }
        """)
        self.assertEqual(len(r["alerts"]), 1)
        self.assertEqual(r["alerts"][0]["classification"], "true_positive")

    def test_recursion_with_sanitizer_base_case(self):
        # 基例清洗：所有终止路径都返回干净值（FP-2 说明的边界情形）
        r = run("""
        fn walk(n, x){
            if (n == 0) { return clean(x); } else { return walk(n-1, x); }
        }
        fn main(){ t=source(); sink(walk(5,t)); }
        """)
        # 基例无条件清洗 => 无真实告警；保守分析可能因“0/多路径”报 0~1 条，
        # 但若报必须被标为可能误报
        for a in r["alerts"]:
            self.assertTrue(a["may_be_false_positive"])

    def test_mutual_recursion(self):
        r = run("""
        fn even(n,v){ if(n==0){return v;} else {return odd(n-1,v);} }
        fn odd(n,v){ if(n==0){return 0;} else {return even(n-1,v);} }
        fn main(){ t=source(); sink(even(4,t)); }
        """)
        self.assertEqual(len(r["alerts"]), 1)

    def test_recursion_terminates_analysis(self):
        # 有界上下文 + 有限格保证终止
        r = run("""
        fn loop_(n,x){
            if (n == 0) { return x; }
            return loop_(n-1, loop_(n-1, x));
        }
        fn main(){ sink(loop_(8, source())); }
        """)
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(len(r["alerts"]), 1)

    def test_k_zero_recursion_terminates(self):
        # 回归：k=0 时 seq[-0:] == seq（非空）曾导致调用串无限增长、不终止
        r = run("""
        fn walk(n, x){
            if (n == 0) { return x; }
            return walk(n-1, x);
        }
        fn main(){ sink(walk(50, source())); }
        """, context_k=0)
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["alerts"]), 1)


class TestBranching(unittest.TestCase):
    def test_guarded_sanitize_constant_flag_is_known_fp(self):
        # FP-2 核心样例：常量 flag=0 时实际只有清洗分支执行，
        # 但不做常量传播，两支都考虑 => 一条可能误报
        r = run("""
        fn main(){
            x = source();
            flag = 0;
            if (flag) { x = x; } else { x = clean(x); }
            sink(x);
        }""")
        self.assertEqual(len(r["alerts"]), 1)
        a = r["alerts"][0]
        self.assertTrue(a["may_be_false_positive"])
        self.assertEqual(a["classification"], "possible_false_positive_branch")

    def test_both_branches_tainted_is_definite(self):
        r = run("""
        fn main(){
            x = source();
            if (c) { y = x; } else { y = x + 1; }
            sink(y);
        }""")
        self.assertEqual(len(r["alerts"]), 1)
        self.assertEqual(r["alerts"][0]["classification"], "true_positive")

    def test_sink_inside_each_branch(self):
        r = run("""
        fn main(){
            x = source();
            if (c) { sink(x); } else { sink(x); }
        }""")
        # 两个不同 sink 位置各一条
        self.assertEqual(len(r["alerts"]), 2)

    def test_clean_then_unrelated_source_in_branch(self):
        r = run("""
        fn main(){
            a = source();
            b = clean(a);
            if (c) { sink(b); } else { d = source(); sink(d); }
        }""")
        # b 干净不报；else 内的 d 报
        lines = sink_lines(r)
        self.assertEqual(len(r["alerts"]), 1)


class TestLoops(unittest.TestCase):
    def test_taint_survives_loop(self):
        r = run("""
        fn main(){
            x = source(); i = 0;
            while (i < 3) { x = x + 1; i = i + 1; }
            sink(x);
        }""")
        self.assertEqual(len(r["alerts"]), 1)
        self.assertEqual(r["alerts"][0]["classification"], "true_positive")

    def test_sanitize_inside_loop_zero_iterations_fp(self):
        # 清洗在循环体内：循环 0 次执行时污点未清洗 => 保守告警（已知 FP）
        r = run("""
        fn main(){
            y = source(); j = 0;
            while (j < 3) { y = clean(y); j = j + 1; }
            sink(y);
        }""")
        self.assertEqual(len(r["alerts"]), 1)
        self.assertTrue(r["alerts"][0]["may_be_false_positive"])
    def test_sanitize_before_loop_is_clean(self):
        r = run("""
        fn main(){
            y = clean(source()); j = 0;
            while (j < 3) { j = j + 1; }
            sink(y);
        }""")
        self.assertEqual(len(r["alerts"]), 0)


class TestUnknownCalls(unittest.TestCase):
    def test_unknown_call_conservative_fp(self):
        r = run("fn main(){ y = mystery(source()); sink(y); }")
        self.assertEqual(len(r["alerts"]), 1)
        self.assertTrue(r["alerts"][0]["may_be_false_positive"])
        self.assertEqual(r["alerts"][0]["classification"],
                         "possible_false_positive")

    def test_unknown_call_with_clean_arg_no_alert(self):
        r = run("fn main(){ y = mystery(42); sink(y); }")
        self.assertEqual(len(r["alerts"]), 0)

    def test_strict_mode_rejects_unknown(self):
        r = run("fn main(){ mystery(1); }",
                **{"conservative_unknown_calls": False})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["type"], "AnalysisError")


class TestPath(unittest.TestCase):
    def test_path_starts_at_source_ends_at_sink(self):
        r = run("fn id(x){return x;} fn main(){ sink(id(source())); }")
        a = r["alerts"][0]
        self.assertEqual(a["path"][0]["kind"], "source")
        self.assertEqual(a["path"][-1]["kind"], "sink")
        kinds = [s["kind"] for s in a["path"]]
        self.assertIn("param_bind", kinds)
        self.assertIn("call_return", kinds)

    def test_path_steps_have_locations(self):
        r = run("fn main(){\n\na = source();\n\nsink(a);\n}")
        a = r["alerts"][0]
        src = next(s for s in a["path"] if s["kind"] == "source")
        self.assertEqual(src["at"].split(":")[1], "3")

    def test_path_is_ordered_through_chain(self):
        r = run("""
        fn a(x){return x;}
        fn b(x){return a(x);}
        fn c(x){return b(x);}
        fn main(){ sink(c(source())); }
        """)
        kinds = [s["kind"] for s in r["alerts"][0]["path"]]
        # 绑定步骤的函数顺序应为 c -> b -> a
        binds = [s["at"].split(":")[0] for s in r["alerts"][0]["path"]
                 if s["kind"] == "param_bind"]
        self.assertEqual(binds, ["c", "b", "a"])

    def test_path_does_not_cross_sanitizer(self):
        r = run("fn main(){ a=source(); b=clean(a); c=b; sink(c); }")
        self.assertEqual(len(r["alerts"]), 0)


class TestEntryAndMultiple(unittest.TestCase):
    def test_no_main_analyzes_all_functions(self):
        r = run("fn helper(){ x=source(); sink(x); }")
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["alerts"]), 1)
        self.assertIsNone(r["entry"])

    def test_multiple_sources_sinks(self):
        r = run("""
        fn main(){
            a = source(); b = source();
            sink(a);
            sink(b);
            sink(100);
        }""")
        self.assertEqual(len(r["alerts"]), 2)

    def test_config_changes_builtin_names(self):
        src = "fn main(){ x = getInput(); log(escape(x)); log(x); }"
        r = run(src, sources=["getInput"], sinks=["log"], sanitizers=["escape"])
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["alerts"]), 1)  # log(escape(x)) clean; log(x) alert


class TestErrors(unittest.TestCase):
    def test_lex_error_structured(self):
        r = run("fn main(){ @ }")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["type"], "LexError")
        self.assertIn("span", r["error"])

    def test_parse_error_structured(self):
        r = run("fn main(){ x = ; }")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["type"], "ParseError")

    def test_arity_error_structured(self):
        r = run("fn f(a){} fn main(){ f(); }")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["type"], "AnalysisError")

    def test_bad_config(self):
        r = run("fn main(){}", context_k=-1)
        self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()
