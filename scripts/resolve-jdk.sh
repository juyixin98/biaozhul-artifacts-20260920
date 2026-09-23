# Resolves a locally installed JDK without root access and exports JAVA / JAVAC.
# Resolution order: $JAVA_HOME, java/javac on PATH, ~/tools/jdk-17.0.20.1+1
# (the locked version used for development), then any ~/tools/jdk-*.
#
# Sourced by build.sh / test.sh / run.sh.

if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/javac" ]; then
  JAVAC="$JAVA_HOME/bin/javac"
  JAVA="$JAVA_HOME/bin/java"
elif command -v javac >/dev/null 2>&1; then
  JAVAC="$(command -v javac)"
  JAVA="$(command -v java)"
else
  for d in "$HOME/tools/jdk-17.0.20.1+1" $HOME/tools/jdk-17* $HOME/tools/jdk-*; do
    if [ -x "$d/bin/javac" ]; then
      JAVAC="$d/bin/javac"
      JAVA="$d/bin/java"
      break
    fi
  done
fi

if [ -z "${JAVAC:-}" ] || [ -z "${JAVA:-}" ]; then
  echo "ERROR: no JDK found. Set JAVA_HOME to a JDK 11+ install" >&2
  echo "       (developed and locked against Temurin 17.0.20.1+1)." >&2
  exit 1
fi
