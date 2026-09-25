// sanitized.tl — taint removed before the sink. No alarm expected.
func clean(v) {
  return sanitize(v);
}

func main() {
  var x = source();
  var y = clean(x);
  sink(y);            // safe: sanitized inside clean()
  sink(x);            // ALARM: original x still tainted
}
