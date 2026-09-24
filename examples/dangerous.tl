// 危险样例：source 经过跨函数传播到达 sink（无条件净化）
func load_config() {
    var raw = read_file("config");
    return raw;
}

func build_query(name) {
    var q = concat("SELECT * FROM users WHERE name='", name, "'");
    return q;
}

func main() {
    var data = load_config();
    var sql = build_query(data);
    query(sql);
}
