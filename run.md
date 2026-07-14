# 运行说明

不要把真实密钥写入仓库文件。请在当前终端设置环境变量，或使用已被
`.gitignore` 排除的本地 `.env`/`key.md` 文件自行加载。

```bash
export MODEL_API_KEY="<your-api-key>"
export MODEL_BASE_URL="<openai-compatible-base-url>"
export MODEL_NAME="<model-name>"

# 只有文件预读取失败、需要 FileSurfer 时才需要单独配置。
export FILE_SURFER_API_KEY="<your-file-model-api-key>"
export FILE_SURFER_BASE_URL="<openai-compatible-base-url>"
export FILE_SURFER_MODEL="<tool-calling-model-name>"
```

运行示例：

```bash
python main.py --input examples/expense_reimbursement
python main.py --input examples/mathorcup_d
```

运行状态和产物位于 `outputs/runs/<run_id>/`。
