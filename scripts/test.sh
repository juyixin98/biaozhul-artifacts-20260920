#!/usr/bin/env bash
# Compile and run all tests with the built-in mini harness. No JUnit/Maven needed.
set -euo pipefail
cd "$(dirname "$0")/.."
./scripts/build.sh
rm -rf out/test-classes
mkdir -p out/test-classes
find src/test/java -name '*.java' > out/test-sources.txt
javac -cp out/classes -d out/test-classes @out/test-sources.txt

TESTS=(
  com.example.watermark.test.VirtualClockTest
  com.example.watermark.test.ExecutorSchedulerTest
  com.example.watermark.test.WatermarkManagerTest
  com.example.watermark.test.WindowTest
  com.example.watermark.test.JsonTest
  com.example.watermark.test.ScenarioTest
  com.example.watermark.test.RandomMonotonicityTest
  com.example.watermark.test.HttpServiceTest
)
java -cp out/classes:out/test-classes com.example.watermark.test.TestRunner "${TESTS[@]}"
