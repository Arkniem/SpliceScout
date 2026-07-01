#!/usr/bin/env bash
# one-time: build a self-contained conda env with llama-cpp-python (CPU) + download the Gemma 4 E4B GGUF
# (it-qat Q4_0, ~5.15GB). Run on a COMPUTE node (modern glibc + internet egress + the anaconda3 module).
set -e
AIH=/data/salomonis-archive/LabFiles/SpliceScout_AI
ENV="$AIH/env"
MODEL="$AIH/models/gemma4-E4B-it-qat-Q4_0.gguf"
source /etc/profile.d/modules.sh 2>/dev/null || true
module load anaconda3 2>/dev/null || true
echo "[setup] $(date) creating conda env at $ENV"
conda create -y -p "$ENV" python=3.10 pip
echo "[setup] $(date) installing llama-cpp-python (CPU PREBUILT wheel -- gemma4 arch + glibc-2.17 compatible)"
# --only-binary=:all: FORCES the prebuilt manylinux2014 CPU wheel; a plain install tries a source build that
# dies on the RHEL7 login node's gcc 4.8.5 (the recipe that actually works on this cluster).
"$ENV/bin/pip" install --no-cache-dir --only-binary=:all: llama-cpp-python \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
echo "[setup] $(date) downloading model -> $MODEL"
if [ ! -s "$MODEL" ]; then
  curl -L --fail --retry 3 -o "$MODEL.part" \
    "https://registry.ollama.ai/v2/library/gemma4/blobs/sha256:e8b6a059ba86947a44ace84d6e5679795bc41862c25c30513142588f0e9dba1d" \
    && mv -f "$MODEL.part" "$MODEL"
fi
"$ENV/bin/python" -c "import llama_cpp; print('[setup] llama_cpp', llama_cpp.__version__)"
ls -la "$MODEL"
echo "SETUP_DONE $(date)"
