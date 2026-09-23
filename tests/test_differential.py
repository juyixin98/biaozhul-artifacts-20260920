"""Acceptance behaviour: reference interpreter vs converted VM.

These are the required scenarios from the task: shadowing, recursion,
escaping closures, and multiple closures sharing updates. Every program is
run both ways and must produce identical output.
"""
import unittest

from tests._helpers import both_outputs


class DifferentialCase(unittest.TestCase):
    def assertAgrees(self, source, expected=None):
        ref, vm = both_outputs(source)
        self.assertEqual(ref, vm,
                         msg=f"reference {ref!r} != vm {vm!r}")
        if expected is not None:
            self.assertEqual(vm, expected)


class TestShadowing(DifferentialCase):
    def test_nested_blocks(self):
        self.assertAgrees(
            "let x=1; print(x);"
            "{ let x=2; print(x); {let x=3; print(x);} print(x);}"
            "print(x);",
            ["1", "2", "3", "2", "1"])

    def test_parameter_shadow_and_restore(self):
        self.assertAgrees(
            "fn f(x){ let x=x+10; {let x=99; print(x);} print(x);}"
            "f(5);",
            ["99", "15"])

    def test_closure_binds_shadowed_outer(self):
        self.assertAgrees(
            "let g=5;"
            "fn f(){ let g=10;"
            "  { let g=20; print(g); }"
            "  print(g);"
            "  return fn(){return g;}; }"
            "print(f()()); print(g);",
            ["20", "10", "10", "5"])


class TestRecursion(DifferentialCase):
    def test_factorial(self):
        self.assertAgrees(
            "fn fact(n){ if(n<=1){return 1;} return n*fact(n-1); }"
            "print(fact(6));", ["720"])

    def test_fibonacci(self):
        self.assertAgrees(
            "fn fib(n){ if(n<2){return n;} return fib(n-1)+fib(n-2); }"
            "print(fib(12));", ["144"])

    def test_mutual_recursion(self):
        self.assertAgrees(
            "fn even(n){ if(n==0){return true;} return odd(n-1); }"
            "fn odd(n){ if(n==0){return false;} return even(n-1); }"
            "print(even(10), odd(7), even(3));",
            ["true, true, false"])

    def test_self_reference_in_body(self):
        self.assertAgrees(
            "fn count(n){ if(n==0){return 0;} return 1+count(n-1); }"
            "print(count(50));", ["50"])


class TestEscapingClosures(DifferentialCase):
    def test_escape_and_outlive_creator(self):
        self.assertAgrees(
            "let gset=nil;"
            "fn install(){ let state=100;"
            "  gset=fn(v){state=v; return state;};"
            "  let read=fn(){return state;};"
            "  print(read()); print(gset(5)); print(read()); }"
            "install(); print(gset(55));",
            ["100", "5", "5", "55"])

    def test_adder_factory_independent_instances(self):
        self.assertAgrees(
            "fn adder(n){ return fn(x){return x+n;}; }"
            "let a5=adder(5); let a10=adder(10);"
            "print(a5(3), a10(3)); print(a5(100));",
            ["8, 13", "105"])

    def test_transitive_capture(self):
        self.assertAgrees(
            "fn outer(){ let secret=42;"
            "  fn middle(){ fn inner(){return secret;} return inner; }"
            "  return middle; }"
            "print(outer()()());", ["42"])

    def test_anonymous_closure_captures_param(self):
        self.assertAgrees(
            "fn make(start){ return fn(){ start=start+1; return start; }; }"
            "let a=make(10); print(a(),a(),a());", ["11, 12, 13"])


class TestSharedUpdates(DifferentialCase):
    def test_counter_shared_cell_not_copy(self):
        # The defining test: two closures must share one updateable cell.
        self.assertAgrees(
            "fn make(){ let c=0;"
            "  fn inc(){ c=c+1; return c; }"
            "  fn get(){ return c; }"
            "  return inc; }"
            "let f=make(); print(f()); print(f());",
            ["1", "2"])

    def test_two_mutators_share_state(self):
        self.assertAgrees(
            "fn bank(){ let bal=100;"
            "  fn dep(x){ bal=bal+x; return bal; }"
            "  fn wd(x){ bal=bal-x; return bal; }"
            "  print(dep(30)); print(wd(50)); print(dep(10));"
            "  print(wd(500)); }"
            "bank();",
            ["130", "80", "90", "-410"])

    def test_three_closures_share(self):
        self.assertAgrees(
            "fn mk(){ let v=0;"
            "  fn a(){v=v+1; return v;}"
            "  fn b(){v=v*2; return v;}"
            "  return fn(sel){ if(sel==0){return a();} return b(); }; }"
            "let p=mk();"
            "print(p(0)); print(p(1)); print(p(0)); print(p(1));",
            ["1", "2", "3", "6"])

    def test_instances_do_not_share(self):
        # shared within a pair of closures, isolated across factory calls
        self.assertAgrees(
            "fn mk(){ let c=0; fn inc(){c=c+1; return c;} return inc; }"
            "let f=mk(); let g=mk();"
            "print(f()); print(f()); print(g()); print(f());",
            ["1", "2", "1", "3"])

    def test_many_same_depth_siblings_distinct(self):
        # Regression: sibling closures share lexical *depth* but must be
        # compiled as distinct functions capturing their own bindings.
        src = [
            "fn factory(start){",
        ]
        for k in range(6):
            src.append(f"  let v{k} = {k + 1};")
        for k in range(6):
            if k % 2 == 0:
                src.append(f"  fn get{k}(){{ return v{k}*10 + start; }}")
            else:
                src.append(f"  fn bump{k}(){{ v{k}=v{k}+1; return v{k}; }}")
        for k in range(6):
            src.append(f"  print({'get' if k%2==0 else 'bump'}{k}());")
        src.append("}")
        src.append("factory(100); factory(200);")
        ref, vm = both_outputs("\n".join(src))
        self.assertEqual(ref, vm)
        # evens k=0,2,4 -> (k+1)*10+start ; odds k=1,3,5 -> (k+1)+1
        self.assertEqual(ref, [
            "110", "3", "130", "5", "150", "7",
            "210", "3", "230", "5", "250", "7"])

    def test_getter_sees_setter_update(self):
        self.assertAgrees(
            "fn pair(){ let v=1;"
            "  let get=fn(){return v;};"
            "  let set=fn(x){v=x;};"
            "  print(get()); set(40); print(get());"
            "  set(v+2); print(get()); }"
            "pair();",
            ["1", "40", "42"])


class TestControlAndOps(DifferentialCase):
    def test_while_accumulate(self):
        self.assertAgrees(
            "let i=0; let acc=0; while(i<5){acc=acc+i; i=i+1;} print(acc);",
            ["10"])

    def test_short_circuit_value_semantics(self):
        self.assertAgrees(
            "print(1 and 2, 0 and 3, 1 or 2, 0 or 5);",
            ["2, 0, 1, 5"])

    def test_integer_division_and_modulo(self):
        self.assertAgrees(
            "print(7/2, -7/2, 7%-3, -7%3);",
            ["3, -3, 1, -1"])

    def test_bool_int_distinct(self):
        self.assertAgrees(
            "print(1==true, true==true, nil==nil, nil==0, 0==false);",
            ["false, true, true, false, false"])


if __name__ == "__main__":
    unittest.main()
