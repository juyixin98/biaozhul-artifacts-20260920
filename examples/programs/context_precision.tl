// context_precision.tl
// identity(v) is called in two contexts: once with taint, once with a
// constant.  Flow-insensitive / k=0 analysis merges these and reports the
// clean sink too.  With k>=2 the contexts are distinct: exactly ONE alarm,
// at the sink following the tainted call.
func identity(v) {
  return v;
}

func main() {
  var t = identity(source());
  sink(t);             // ALARM expected

  var c = identity("clean");
  sink(c);             // safe (provided context depth distinguishes callers)
}
