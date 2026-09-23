// bool_logic.sl —— bool 类型、逻辑运算、条件合流上的确定赋值
fn is_leap(int year): bool {
    bool div4 = year % 4 == 0;
    bool div100 = year % 100 == 0;
    bool div400 = year % 400 == 0;
    return div4 && (!div100 || div400);
}

fn main() {
    print is_leap(2000);   // true
    print is_leap(1900);   // false
    print is_leap(2024);   // true
    print is_leap(2026);   // false

    int result;            // 声明但暂不初始化
    bool cond = 3 < 10;
    if (cond) {
        result = 11;
    } else {
        result = 22;
    }
    // 合流点：两条路径都已给 result 赋值，读取合法
    print result;          // 11
}
