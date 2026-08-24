#!/usr/bin/env bash
# Run a batch end-to-end and print metrics.
#   ./scripts/run_batch.sh dev       # tune against this
#   ./scripts/run_batch.sh holdout   # run ONCE, report these numbers
set -euo pipefail
SET="${1:-dev}"
python -m app.detection.batch_scanner --set "$SET"
