// opaque_call.tl
// `external` has no function body in the program.  The analyzer cannot see
// what it does, so it conservatively assumes the result is tainted when an
// argument is tainted.  One alarm expected; the call is also listed under
// `opaque_calls` in the JSON result.
func main() {
  var a = external(source());
  sink(a);            // ALARM (conservative)

  var b = external(123);
  sink(b);            // safe: no tainted argument
}
