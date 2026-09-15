#!/usr/bin/env bash
set -euo pipefail

# This script consumes already-exported model variables; it never prints or
# writes their values. Each run must pass the complete audit before packaging.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

for variable in MODEL_API_KEY MODEL_BASE_URL MODEL_NAME; do
  if [[ -z "${!variable:-}" ]]; then
    echo "缺少环境变量: ${variable}" >&2
    exit 2
  fi
done

python utils/preflight.py --input examples/mathorcup_d
python utils/preflight.py --input examples/expense_reimbursement
python utils/preflight.py --input 城市多模态数据集

run_case() {
  local input_path="$1"
  shift
  local log_file
  log_file="$(mktemp "${TMPDIR:-/tmp}/competition-run.XXXXXX")"
  trap 'rm -f "$log_file"' RETURN
  python main.py --input "$input_path" "$@" | tee "$log_file"
  RUN_DIR="$(sed -n 's/^运行完成: //p' "$log_file" | tail -1)"
  if [[ -z "$RUN_DIR" || ! -d "$RUN_DIR" ]]; then
    echo "未能从运行输出定位 run_dir: ${input_path}" >&2
    exit 3
  fi
  python utils/run_audit.py "$RUN_DIR"
}

run_case examples/mathorcup_d
MATH_RUN="$RUN_DIR"
run_case examples/expense_reimbursement --injection-profile recovery-demo
EXPENSE_RUN="$RUN_DIR"
python utils/injection_audit.py "$EXPENSE_RUN"
run_case 城市多模态数据集
URBAN_RUN="$RUN_DIR"

python utils/competition_metrics.py \
  "$MATH_RUN" "$EXPENSE_RUN" "$URBAN_RUN" --output-dir evidence/metrics
python utils/export_demo_evidence.py \
  "$MATH_RUN" "$EXPENSE_RUN" "$URBAN_RUN" --output-dir evidence
python utils/package_submission.py \
  --run-dir "$MATH_RUN" --run-dir "$EXPENSE_RUN" \
  --run-dir "$URBAN_RUN" \
  --output submission/competition_submission.zip

echo "比赛收尾完成: ${MATH_RUN} ${EXPENSE_RUN} ${URBAN_RUN}"
echo "提交包: submission/competition_submission.zip"
