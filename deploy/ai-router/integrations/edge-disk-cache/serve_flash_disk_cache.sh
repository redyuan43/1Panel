#!/usr/bin/env bash
set -Eeuo pipefail

readonly RUNTIME_ROOT="${QWEN38_RUNTIME_ROOT:-/home/admin/github/qwen3.8_flash_next}"

# shellcheck source=/dev/null
source "${RUNTIME_ROOT}/scripts/common.sh"

"${WORKSPACE}/scripts/preflight_runtime.sh"

readonly SPLITTING_OPS='["vllm::unified_attention_with_output","vllm::unified_mla_attention_with_output","vllm::mamba_mixer2","vllm::mamba_mixer","vllm::short_conv","vllm::qwen3_8_flash_next_ple_short_conv","vllm::qwen3_8_flash_next_qsa_with_output","vllm::linear_attention","vllm::qwen_gdn_attention_core","vllm::qwen_gdn_attention_core_fused_norm_packed","vllm::sparse_attn_indexer","vllm::ple_mmap_lookup"]'

: "${KV_CACHE_DIR:?KV_CACHE_DIR must be configured}"
: "${KV_CACHE_BYTES:?KV_CACHE_BYTES must be configured}"
: "${KV_CACHE_BUFFER_SLOTS:?KV_CACHE_BUFFER_SLOTS must be configured}"

install -d -m 0700 "${KV_CACHE_DIR}"

prefix_cache_arg="--no-enable-prefix-caching"
[[ "${PREFIX_CACHE}" == "1" ]] && prefix_cache_arg="--enable-prefix-caching"

allow_long=0
hf_override_args=()
if [[ "${YARN:-0}" == "1" ]]; then
    allow_long=1
    readonly YARN_OVERRIDES='{"text_config":{"rope_parameters":{"mrope_interleaved":true,"mrope_section":[11,11,10],"rope_type":"yarn","rope_theta":10000000,"partial_rotary_factor":0.25,"factor":4.0,"original_max_position_embeddings":262144}}}'
    hf_override_args=(--hf-overrides "${YARN_OVERRIDES}")
    speculative_config="{\"method\":\"mtp\",\"num_speculative_tokens\":${MTP},\"max_model_len\":${CTX}}"
else
    speculative_config="{\"method\":\"mtp\",\"num_speculative_tokens\":${MTP}}"
fi

kv_transfer_config="$(
    jq -cn \
        --argjson cpu_bytes "${KV_CACHE_CPU_STAGING_BYTES:-1073741824}" \
        --argjson disk_bytes "${KV_CACHE_BYTES}" \
        --argjson buffer_slots "${KV_CACHE_BUFFER_SLOTS}" \
        --argjson lazy_offload "${KV_CACHE_LAZY_OFFLOAD:-true}" \
        '{
            kv_connector: "SimpleCPUOffloadConnector",
            kv_role: "kv_both",
            kv_connector_extra_config: {
                cpu_bytes_to_use: $cpu_bytes,
                lazy_offload: $lazy_offload,
                kv_offload_backend: "disk",
                disk_path: "/kv-cache/qwen38-prefix",
                disk_capacity_bytes: $disk_bytes,
                disk_buffer_slots: $buffer_slots,
                use_page_cache: false
            }
        }'
)"

exec sudo -n docker run --rm \
    --name "${CONTAINER}" \
    --gpus all \
    --ipc host \
    --shm-size 16g \
    --publish "${HOST}:${PORT}:8000" \
    --volume "${MODEL_DIR}:/model:ro" \
    --volume "${KV_CACHE_DIR}:/kv-cache" \
    --env HF_HUB_OFFLINE=1 \
    --env TRANSFORMERS_OFFLINE=1 \
    --env VLLM_NO_USAGE_STATS=1 \
    --env VLLM_PLE_MMAP=1 \
    --env "VLLM_PLE_MMAP_WORKERS=${PLE_WORKERS}" \
    --env "VLLM_PLE_MMAP_PREWARM=${PREWARM}" \
    --env "VLLM_QSA_EXACT_TOPK=${EXACT_TOPK}" \
    --env VLLM_USE_FLASHINFER_SAMPLER=1 \
    --env VLLM_USE_SIMPLE_KV_OFFLOAD=1 \
    --env "VLLM_ALLOW_LONG_MAX_MODEL_LEN=${allow_long}" \
    "${IMAGE}" \
    /model \
    --served-model-name "${SERVED_MODEL_NAME}" \
    --host 0.0.0.0 \
    --port 8000 \
    --load-format safetensors \
    --max-model-len "${CTX}" \
    --max-num-seqs "${SEQS}" \
    --gpu-memory-utilization "${GPU_MEM}" \
    "${prefix_cache_arg}" \
    --enable-chunked-prefill \
    --max-num-batched-tokens 8192 \
    -cc.cudagraph_mode=PIECEWISE \
    "-cc.splitting_ops=${SPLITTING_OPS}" \
    --no-enable-flashinfer-autotune \
    --kv-cache-dtype "${KV_DTYPE}" \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_coder \
    --reasoning-parser qwen3 \
    "${hf_override_args[@]}" \
    --speculative-config "${speculative_config}" \
    --kv-transfer-config "${kv_transfer_config}"
