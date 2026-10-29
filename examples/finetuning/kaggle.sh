#!/usr/bin/env bash
# Run inside an already authorized Kaggle GPU session; this script starts no notebook or paid job.
set -euo pipefail
: "${TESSERA_CHECKOUT:?Set the path to a checked-out Tessera revision}"
: "${FINETUNE_OUTPUT:?Set a new task-owned output directory}"
: "${FINETUNE_RECIPE:?Set a recipe path; choose tiny-smoke.json or an authorized pretrained recipe}"
cd "$TESSERA_CHECKOUT"
python - <<'PY'
import torch
if not torch.cuda.is_available():
    raise SystemExit('Enable an authorized compatible GPU session first')
print({'gpu': torch.cuda.get_device_name(0), 'torch': torch.__version__})
PY
# Install optional dependencies explicitly before invoking this script; preserve Kaggle's CUDA torch.
# For pretrained models, attach already permitted pinned HF cache files. No credentials belong here.
python -m examples.finetuning.run prepare --output "$FINETUNE_OUTPUT/data"
python -m examples.finetuning.run train --dataset "$FINETUNE_OUTPUT/data" \
  --output "$FINETUNE_OUTPUT/run" --recipe "$FINETUNE_RECIPE" --device cuda
