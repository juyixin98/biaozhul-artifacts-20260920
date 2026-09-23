// 06_loops_strings.slang — loops, short-circuit logic and string values.
let words = "";
let i = 0;
while (i < 4) {
  words = words + "ab";
  i = i + 1;
}
print(words);
print(17 / 5);
print(-17 / 5);
print(17 % 5);
print(1 < 2);
print(false || "default");
print(true && "yes");
print(null);
let guarded = fn(a, b) {
  if (b != 0 && a / b > 2) { return "big"; }
  return "safe";
};
print(guarded(10, 0));
print(guarded(10, 2));
