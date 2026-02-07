---
name: train-run
description: Pre-flight checklist and orchestration for launching a nanochat training run. Validates data, tokenizer, model config, and generates the training command. Use before every training run to catch issues early.
---

# Training Run Checklist

When this skill is invoked, execute the following checklist **in order**. Stop at the first failure and report the issue. Do not skip steps.

## Phase 1: Data Validation

### Step 1 — Verify dataset exists and is healthy
```bash
python nanochat/dataset.py --validate
python nanochat/dataset.py --info
```
- Confirm all parquet files pass validation (readable, have 'text' column)
- Report: shard count, total documents, total size
- **FAIL if:** no parquet files found, any shard fails validation, total size < 1MB

### Step 2 — Check for secrets in data
```bash
python dev/sanitize_iac.py --dry-run
```
- Scan for AWS keys, SSH keys, API tokens, real IPs, base64 blobs
- **FAIL if:** any secrets detected. Do NOT proceed to training with contaminated data.
- **Fix:** Run `python dev/sanitize_iac.py` (without --dry-run) to remediate, then re-validate.

## Phase 2: Tokenizer Validation

### Step 3 — Verify tokenizer exists and is valid
- Check that `~/.cache/nanochat/tokenizer/` contains either `tokenizer.json` or `tokenizer.pkl`
- If no tokenizer exists, prompt the user: "No tokenizer found. Run `python dev/train_iac_tokenizer.py` first?"

### Step 4 — Check compression ratio
```bash
python dev/train_iac_tokenizer.py --validate
```
- Target compression ratio: 3.0-4.0x for IaC text
- **WARN if:** ratio < 2.5x (tokenizer may need retraining with more domain data)
- **WARN if:** ratio > 5.0x (tokenizer may be undertrained or vocab too large)

## Phase 3: Model Sizing

### Step 5 — Compute param-to-token ratio
Using the dataset info from Step 1, calculate:
- **Estimated total tokens** = total_size_bytes * compression_ratio (from Step 4)
- **Recommended model:**
  - < 100M tokens -> d12 (124M params, n_layer=12, n_embd=768)
  - 100M-500M tokens -> d16 (~200M params, n_layer=16, n_embd=1024)
  - 500M+ tokens -> d24 (~350M params, n_layer=24, n_embd=1536)
- **Param-data ratio** = recommended_params / estimated_tokens
- **WARN if:** ratio > 10 (extreme overfitting risk, suggest data augmentation)
- **WARN if:** ratio > 5 (moderate overfitting risk, suggest aggressive regularization)

### Step 6 — Report training estimate
Present a summary table:
```
Dataset:        X shards, Y documents, Z MB
Tokenizer:      vocab_size, compression ratio
Model:          dN (params), sequence_len
Param/Token:    ratio (risk level)
Est. epochs:    at Chinchilla-optimal
```

## Phase 4: Launch Preparation

### Step 7 — Select training script
Ask the user which training configuration to use:
- `bash runs/speedrun_iac.sh` — IaC-specific speedrun (recommended for IaC-GPT)
- `bash runs/speedrun.sh` — General speedrun
- `bash runs/iac_speedrun.sh` — Legacy IaC orchestration
- Custom — let user specify flags

### Step 8 — Confirm and launch
Present the final command and ask for explicit confirmation before running.
Remind the user:
- Monitor val loss for overfitting (3+ epochs without improvement = stop)
- Checkpoints save automatically
- Use Ctrl+C for graceful shutdown (checkpoint will be saved)

## Quick Reference
If all checks pass and the user wants to skip the interactive parts:
```bash
python nanochat/dataset.py --validate && python dev/sanitize_iac.py --dry-run && bash runs/speedrun_iac.sh
```
