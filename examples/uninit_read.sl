// uninit_read.sl —— 演示“局部量初始化”检查（合法源码）
// 本程序自身通过验证；对其字节码做单字节变异（例如把条件中的
// PUSH 立即数改掉，或把 STORE 操作码改成 NOP），验证器就会在
// 合流点/读取点报 LOCAL_UNINITIALIZED 或 STACK_*，并给出最短路径。
fn maybe_assign(bool flag): int {
    int x;                 // 不初始化
    if (flag) {
        x = 42;
    } else {
        x = 7;
    }
    return x;              // 合流：两路都写了，合法
}

fn main() {
    print maybe_assign(true);    // 42
    print maybe_assign(false);   // 7
}
