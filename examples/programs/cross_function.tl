// cross_function.tl
// Taint flows main -> get_data -> wrap -> sink through return values.
// Two distinct call sites give two contexts; one alarm expected.
func get_data() {
  return source();
}

func wrap(p) {
  var q = get_data();
  return q;
}

func helper(x) {
  sink(x);          // x tainted through wrap's parameter
}

func main() {
  var a = wrap(source());
  helper(a);
  var b = get_data();
  sink(b);
}
