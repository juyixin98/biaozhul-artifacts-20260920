// early_return.sl —— 分支中的“异常返回”（early return）
// 验证器必须确认：无论是否提前返回，返回值类型一致，
// 且合流点不会读到未初始化的局部量。
fn classify(int x): int {
    if (x < 0) {
        return -1;          // 异常返回：负数
    }
    if (x == 0) {
        return 0;           // 异常返回：零
    }
    return 1;               // 正常返回：正数
}

// 递归版（深度大于燃料时的运行期错误路径也可由此产生）
fn fact(int n): int {
    if (n <= 1) {
        return 1;           // 基线提前返回
    }
    return n * fact(n - 1);
}

fn main() {
    print classify(-7);     // -1
    print classify(0);      // 0
    print classify(42);     // 1
    print fact(5);          // 120
}
