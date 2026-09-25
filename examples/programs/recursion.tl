// recursion.tl
// echo(n, v) recursively decrements n and returns v unchanged.
// Taint survives the recursive call -> one alarm expected.
// With k=2 the repeated contexts fold onto each other (bounded analysis);
// with k=0 every call to echo shares one context.
func echo(n, v) {
  if (n <= 0) {
    return v;
  }
  return echo(n - 1, v);
}

func main() {
  var x = source();
  var y = echo(5, x);
  sink(y);
}
