# Optional, prebuilt image for llm_d_bench.model_cache's download/delete
# Kubernetes Jobs (see specs/changes/model-cache-design.md section 5).
#
# By default those Jobs run on plain `python:3.12-slim` (always pullable, no
# build/push step required) and install huggingface_hub at container start
# (see jobs.py's _download_command). Building and pushing this image instead
# is purely an optimization for faster/offline Job starts — pip install is
# skipped entirely if the image already satisfies the requirement.
#
# Build & push (adjust the registry/tag to match your environment, then set
# MODEL_CACHE_DOWNLOADER_IMAGE to the pushed tag):
#   docker build -t <registry>/model-cache-downloader:latest docker/model-cache-downloader
#   docker push <registry>/model-cache-downloader:latest

FROM python:3.12-slim

RUN pip install --no-cache-dir "huggingface_hub[cli,hf_transfer]"

# The container just runs `hf download ...` or `rm -rf ...`;
# the actual command/args are supplied by the Job manifest (see
# llm_d_bench/model_cache/jobs.py), so no ENTRYPOINT/CMD is needed here.
