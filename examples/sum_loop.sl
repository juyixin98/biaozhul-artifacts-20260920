// sum_loop.sl —— 循环求和（产生回边）
// 计算 1+2+...+10 = 55
//
// 语法约定（完整 EBNF 见 docs/LANGUAGE.md）：
//   fn 名字(类型 形参, ...): 返回类型 { ... }
fn sumto(int n): int {
    var i = 1;
    var acc = 0;
    while (i <= n) {
        acc = acc + i;
        i = i + 1;
    }
    return acc;
}

fn main() {
    print sumto(10);
}
