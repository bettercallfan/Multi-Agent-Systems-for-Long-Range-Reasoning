export MODEL_API_KEY="sk-8b89c3d813844f9390c440905aa26b20"
export MODEL_BASE_URL="https://ws-f2bmqv8wy5et87sn.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
export MODEL_NAME="qwen3.5-35b-a3b"

export FILE_SURFER_MODEL="qwen-plus"
export FILE_SURFER_API_KEY="sk-8b89c3d813844f9390c440905aa26b20"
export FILE_SURFER_BASE_URL="https://ws-f2bmqv8wy5et87sn.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"

python main.py --input examples/mathorcup_d

python main.py --input examples/expense_reimbursement
