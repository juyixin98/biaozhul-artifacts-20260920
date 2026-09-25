// loop_and_branch.tl
// Taint introduced only on one loop iteration; conditions are not
// interpreted, so the analyzer reports the post-loop sink (true positive:
// iteration i==1 is genuinely reachable for the written program).
func main() {
  var x = "clean";
  var i = 0;
  while (i < 3) {
    if (i == 1) {
      x = source();
    }
    i = i + 1;
  }
  sink(x);
}
