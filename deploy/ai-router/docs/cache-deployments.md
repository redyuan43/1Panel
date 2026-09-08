# Cache deployment catalog

`config/cache-deployments.yaml` is the machine-readable inventory for the
AI Router cache deployment console. It records the declared cache architecture,
service ownership, persistent paths, validation state and safe management
boundary for AI, Edge, NX3, NX4 and AGX.

The catalog is not a runtime launcher. It contains no credentials and does not
grant the Control service SSH or systemd access. Model identity, context,
modality, health and current endpoint state continue to come from the live
Registry and Fleet probes.

## Evidence boundaries

- Router affinity means that a request returned to a deployment. It does not
  prove a KV-cache hit.
- vLLM APC counters describe GPU prefix reuse.
- LMCache service health and vLLM connector attachment are separate states.
- Edge external-hit counters require matching semantic output and disk I/O
  evidence.
- NX3, NX4 and AGX gateway events require native cached-token counters and TTFT
  evidence. `miss_saved` alone is not a cache hit.
- A declared or validated capability is not displayed as currently healthy
  unless the live target probe agrees.

## Current strategies

- AI: GPU APC, then LMCache CPU DRAM, then recompute.
- Edge: GPU APC, then native vLLM DiskBackend, then recompute.
- NX3 and AGX: llama.cpp checkpoint, then gateway-managed disk snapshot, then
  recompute.
- NX4: the same gateway snapshot architecture is planned. Until the isolated
  build and semantic acceptance pass, the console must keep it marked planned.

The console reuses the existing endpoint enable and automatic-routing actions
for AI and Edge. AI LMCache configuration continues through the existing policy
draft, validation and activation workflow and explicitly requires a controlled
service restart. Remote restart, cache deletion and remote build are not
Control-page capabilities.
