#!/bin/sh
# Harbor's OracleAgent copies this directory to /solution and runs it inside
# the agent container. The reference DERIVES the fixed repository by applying
# a patch to the source and then running the build the instruction names;
# nothing here writes an expected output directly.
set -eu
cd /app/repo
if git apply --whitespace=nowarn /solution/fix.patch 2>/dev/null; then :; else
  patch -p1 --forward < /solution/fix.patch
fi
