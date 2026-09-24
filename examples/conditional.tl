// 条件净化样例：
//   - validate(x) 为真的 then 分支：x 可信，exec 安全（不报告）
//   - 为假的 else 分支：x 未净化，exec 危险（报告，definite）
func main() {
    var x = input();
    if (validate(x)) {
        exec(x);
    } else {
        exec(x);
    }
}
