// conditional_sanitize.tl
// x is sanitized only on the true branch.  Because conditions are not
// interpreted, the merge still considers x tainted -> ONE EXPECTED FALSE ALARM.
func main() {
  var x = source();
  if (x == "safe") {
    x = sanitize(x);
  } else {
    x = x;
  }
  sink(x);   // reported: may-taint at the branch merge (known false positive)
}
