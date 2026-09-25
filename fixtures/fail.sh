#!/usr/bin/env bash
# Fixture: fail-fixture
#
# Deliberately exits non-zero and writes no model.bin. Used to assert that a
# failed build is reported with an exit code and never publishes an artifact.
set -u

echo "fail-fixture: simulating a broken build" >&2
echo "some diagnostic line" >&2
exit 7
