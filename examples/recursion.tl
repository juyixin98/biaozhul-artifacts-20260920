// 递归样例：递归到基准情形时读取 source，污点沿递归返回值一路传到 sink
func crawl(depth) {
    if (depth == 0) {
        return input();
    }
    return crawl(depth - 1);
}

func main() {
    var payload = crawl(4);
    send(payload);
}
