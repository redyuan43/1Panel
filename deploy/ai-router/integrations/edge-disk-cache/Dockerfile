ARG BASE_IMAGE=qwen38-flash-dgx:e655b7d
FROM ${BASE_IMAGE}

ARG VLLM_SITE_PACKAGES=/usr/local/lib/python3.12/dist-packages

COPY patch_simple_kv_disk.py /tmp/patch_simple_kv_disk.py
RUN python3 /tmp/patch_simple_kv_disk.py "${VLLM_SITE_PACKAGES}" \
    && rm /tmp/patch_simple_kv_disk.py

LABEL ai.1panel.edge-disk-cache="qsa-ring-exclusion-load-stream-sync"
