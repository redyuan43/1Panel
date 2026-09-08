# SkillHub Video Reference Archive

This directory is a reference-only snapshot of the video-related WorkBuddy
skills installed on `ivan-laptop` at:

`C:\Users\Ivan\.workbuddy\skills`

Snapshot date: 2026-09-08.

## Contents

- `raw/` contains the 19 requested skill directories plus the supplemental
  `minimax-h3-image-to-video` alias discovered during the audit.
- `MANIFEST.json` records source metadata, logical mappings, built-in skills,
  license evidence, per-file SHA-256 hashes, and known operational risks.
- `deploy/ai-router/ai_router/media_service/prompt_profiles.json` contains the
  original, minimal rules distilled for SIYUAN's local H3 workflow.

Excel, Word, and PDF generation are WorkBuddy built-in capabilities. They are
recorded in the manifest but their `app.asar` implementation is not copied.

## Isolation Rules

The files under `raw/` are untrusted third-party reference material:

- Router and media-service runtime code must not import, execute, or dynamically
  load anything under `raw/`.
- The archived `videogen.py` files are intentionally non-executable.
- No credential store, API key, generated task output, download, or private
  workstation state is included.
- Example configuration files contain placeholders only and are not runtime
  configuration.
- Network-capable scripts in the archive can upload local media, submit paid
  generation tasks, poll results, download artifacts, open a browser, and write
  a user configuration file. They must not be run from this repository.
- Runtime prompt behavior must come from the distilled JSON profile, not from
  these archived scripts or their external service.

## Licensing

No standalone license file was present in any captured directory.
`cinematic-shot-prompt-expert/SKILL.md` declares `MIT`; the other captured
skills do not declare a license in their top-level metadata. The archive is
therefore for internal review and traceability unless the original publisher's
terms independently permit redistribution.
