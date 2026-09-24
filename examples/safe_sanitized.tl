// 安全样例：输入在进入 sink 前被净化
func load_config() {
    var raw = read_file("config");
    var safe = escape(raw);
    return safe;
}

func main() {
    var data = load_config();
    query(data);
}
