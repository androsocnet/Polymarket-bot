#!/usr/bin/env bash
set -u
cd '/mnt/c/Users/Andro Saptajulianto/Downloads/Polymarket-phase2b-fix' || exit 1
export PYTHONPATH=src
PY=.venv/bin/python
targets="$*"
if [ -z "$targets" ]; then
  targets=$(ls tests/test_*.py | sed 's#/#.#; s#\.py$##' | tr '\n' ' ')
fi
$PY -m unittest $targets 2>&1
