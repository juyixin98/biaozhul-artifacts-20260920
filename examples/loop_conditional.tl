// 循环 + 条件净化样例（展示 MAYBE / conditional 报告）
func main() {
    var msg = input();
    var i = 0;
    while (i < 3) {
        if (validate(msg)) {
            msg = escape(msg);
        }
        i = i + 1;
    }
    sink(msg);
}
