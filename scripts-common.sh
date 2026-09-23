# Shared setup for build.sh / test.sh / run.sh.
# Resolves JAVA / JAVAC / JAR: explicit env vars win, then PATH, then a
# JDK unpacked under $HOME/jdks (any jdk-17* or temurin* directory).

if [[ -z "${JAVA:-}" ]]; then
  if command -v java >/dev/null 2>&1; then
    JAVA="$(command -v java)"
  else
    _candidate="$(ls -d "$HOME"/jdks/jdk-17* "$HOME"/jdks/temurin* 2>/dev/null | head -1 || true)"
    if [[ -n "$_candidate" && -x "$_candidate/bin/java" ]]; then
      JAVA="$_candidate/bin/java"
    fi
  fi
fi
if [[ -z "${JAVAC:-}" && -n "$JAVA" ]]; then
  JAVAC="$(dirname "$JAVA")/javac"
fi
if [[ -z "${JAR:-}" && -n "$JAVA" ]]; then
  JAR="$(dirname "$JAVA")/jar"
fi
unset _candidate

if [[ -z "${JAVA:-}" || ! -x "$JAVA" ]]; then
  echo "ERROR: no Java 17+ found. Set JAVA_HOME or JAVA/JAVAC (see README)." >&2
  exit 1
fi
