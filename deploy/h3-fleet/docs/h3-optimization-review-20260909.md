# H3 optimization review, 2026-09-09

This is a read-only upstream review, not an installation or a local quality
benchmark. Existing three-way production preview validation continues unchanged.

## Observed local baseline

- ComfyUI: `250b2e9`, version 0.34.0, commit dated 2026-09-04.
- T8 nodes: `5ff46c2`, release 1.3.3, dated 2026-08-08; clean tracked tree.
- Executed transformer: `minimax_h3_fl2va_int8_convrot.safetensors`.
- Executed LoRA: `t8star_minimax_h3_turbo_4step_ema_comfyui.safetensors`.
- Sampler: T8 `dual_clock_euler`, `native_flow`, four steps, shifts 12/3.
- Text encoder: Qwen3-VL-32B NVFP4 AWQ; video VAE FP16; audio VAE FP32.
- The LightX2V four-step v1.0 768p LoRA filename already exists on Ivan, but
  the tested Studio preview graph does not load it. Presence is not verification
  of provenance, completeness, correct integration or output quality.

## Candidates and boundaries

### LightX2V Turbo v1.0: first low-disruption comparison

The author supplies four- and eight-step 768p FL2VA/T2VA adapters. The 768p
variants use video/audio shifts 6/3, unlike the current 12/3 configuration.
The author's Studio now uses the eight-step 768p variant and reports improved
audio/video quality. Do not swap only the filename or assume eight steps means
twice the end-to-end latency. Use the matching author workflow and measure.

Sources: [model card](https://huggingface.co/lightx2v/Minimax-h3-Turbo),
[model specifications](https://github.com/ModelTC/Minimax-H3-Turbo).

### OpenVDN: recent architecture-level acceleration

The authors announce a September 6 release with hybrid linear/softmax attention.
Their 11.23-second result generates 14.4 seconds at 768p using eight B200s and
eight denoising steps. It excludes loading, warm-up, VAE decoding and encoding;
it is not an end-to-end result or a forecast for Ivan's 3060/4060 Ti GPUs.

The T8 upstream now documents v1.75.0 integration and two-pass latent upscaling,
including a 4060 Ti 16GB compatibility test. Its pruned-mode nine-route tests
use only 320x192x39 and some approach 290 MiB free VRAM: they do not validate
our 362-frame envelope or three-way concurrency. VDN cannot be blindly stacked
with generic EMA, SLA, VSA, Sol attention or another model override.

Sources: [OpenVDN](https://huggingface.co/OpenVDN/vdn-minimax-h3),
[T8 integration](https://github.com/T8mars/comfyui-minimax-h3-audio-T8#openvdn推荐的-8-步路线),
[T8 model package](https://huggingface.co/t8star/Vdn-Minimax-H3-Comfy).

### People realism: quality rather than acceleration

fal's Realism People LoRA targets faces, skin texture and expressions, publishes
19 same-prompt/seed on/off comparisons, and supports native audio. Its author
suggests 0.6-0.8 strength for a lighter effect. This is relevant to the pink
fashion prompt, but compatibility and quality alongside our Turbo route need
independent validation. The Facial Realism CloseUp adapter is another candidate;
its author explicitly labels it experimental and warns about artifacts.

Sources: [fal People LoRA](https://huggingface.co/fal/MiniMax-H3-Realism-People-LoRA),
[CloseUp](https://huggingface.co/prithivMLmods/MiniMax-H3-Facial-Realism-CloseUp).

### Other candidates: lower priority

- FastH3 Preview v1 uses four forwards and trained 90% VSA sparsity. Its author
  warns that difficult motion, detail and audio can fall below base H3, and only
  T2VA is distilled. The published multi-GPU path requires a GPU count dividing
  56 heads, so our three independent workers are not a supported three-way
  tensor/sequence-parallel configuration by implication.
- MATLOWAI Fused Turbo combines a ref-delta base, Turbo and a motion-style LoRA
  into a 21GB INT8 checkpoint. Its bake can avoid live-LoRA backup allocations,
  but the published timings use a 96GB RTX PRO 6000 and change several variables.
  A baked-in style and speed claim are not general quality improvements.
- Kijai W4A8 is experimental. INT8 VAE claims about 1.5x decode speed, not whole
  pipeline speed. It requires compatible ComfyUI and can otherwise output black.
- NVFP4 diffusion/fused-MLP benchmarks target Blackwell. Do not transfer their
  speedups to Ampere/Ada. The existing NVFP4 AWQ text encoder is a different path.

Sources: [FastH3](https://huggingface.co/FastVideo/FastVideo-FastH3-4-step-Preview-v1-VSA-DataFree),
[Fused Turbo](https://huggingface.co/MATLOWAI/minimax-h3-fused-turbo-int8-convrot),
[Kijai experiments](https://huggingface.co/Kijai/MiniMax-H3-experimental),
[optimization suite measurements](https://github.com/ByronLeeeee/ComfyUI-MiniMax-H3-Optimization-Suite/blob/main/EVALUATION_REPORT.md),
[NVFP4 hardware boundary](https://huggingface.co/FenomAI/MiniMax-H3),
[Comfy packaging](https://huggingface.co/Comfy-Org/MiniMax-H3).

## Proposed next comparison, not started

Keep production's proven four-step route. Use the saved 15-second pink prompt,
the same processed IR, seed, aspect ratio and output shape for controlled
comparisons, changing one candidate bundle at a time. Pair each adapter with
its required sampler and schedule; this is a comparison of coherent recipes,
not an arbitrary LoRA swap. Start with LightX2V, then evaluate a people LoRA;
audit VDN in isolation before any full-duration run or concurrency promotion.

Record model hashes, applied adapter targets, actual sampler execution, load /
sampling / decode / total latency, host RAM, swap, GPU VRAM, allocation failures,
audio and visual comparisons. Review face/hands, mirror consistency, clothing
transition, shot coverage and temporal flicker. Preserve original prompt text;
resolve its sleepwear-to-final-outfit transition in the structured script rather
than interpreting wardrobe consistency as a ban on its explicitly requested change.

The official model card emphasizes structured Context IR and says the hosted
Regenerate-2K component is not open-sourced. Community upscaling is not proof of
official 2K parity, and the prompt's word "4K" does not establish a 4K output.
Source: [MiniMax model card](https://huggingface.co/MiniMaxAI/MiniMax-H3).
