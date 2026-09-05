# WorkBuddy Media Integration

## Installed Client

- Device: `ivan-laptop`, Windows 11, WorkBuddy 5.5.3, Python 3.12.
- Skill: `C:\Users\Ivan\.workbuddy\skills\siyuan-media`.
- Credential directory: `%LOCALAPPDATA%\SIYUAN\Media`.
- Public package: `~/.local/share/ai-router-media/dist/siyuan-media-1.0.0.zip`
  on AI, also in the Windows `SIYUAN\MediaInstaller` directory.
- Router client: `workbuddy-ivan-laptop`, public disclosure, media grants
  `siyuan-image` and `siyuan-video`, no administrator or Qwen grant.
- The existing account validator also requires `models: ["siyuan/auto"]`.
  This is a dedicated media-use account, not a strictly media-only API key.
- Limits: 30 requests/minute, 100,000 text tokens/minute, one active request.
  These request limits are not a daily media budget or a per-person spending cap.

Use the local `siyuan-media` Skill in a new WorkBuddy conversation. Refresh the
Skill selector if it was already open. For example:

> Use siyuan-media to generate a square product photo of a green ceramic mug.

> Use siyuan-media to change the mug in this image to yellow.

For videos, each completed stage is returned through Router to the client.
Review the exact output, explicitly approve its `output_id`, then separately
instruct the client to start the successor. The skill must stop after each
stage. A broad video request is not approval of unseen results.

## Credential Boundary

The public ZIP contains exactly four files: `SKILL.md`, `references/usage.md`,
`scripts/media.py`, and `scripts/install.py`. No key, receipt, configuration or
user artifact is packaged. WorkBuddy's upload-Skill flow is not used.

Windows stores the key as a CurrentUser DPAPI blob, with directory access
restricted to the Windows user and SYSTEM. The plaintext server backup is
outside Git at `~/.config/ai-router-media/clients/workbuddy-ivan-laptop.json`,
mode 0600 under a 0700 directory. It is not placed in prompts, model settings,
command-line arguments or tool output.

The local helper decrypts in process and injects Authorization only into requests
to the fixed Router origin. It ignores environment/system proxies and refuses
redirects. It strips credential echoes, signed download URLs and image Base64
from model-visible JSON. Downloads are authenticated, size/hash checked, and
published without overwriting existing files.

This prevents credential submission in the intended workflow; it is not a
sandbox against malicious code running as the same Windows user. Such code can
invoke DPAPI too. A stronger trust boundary requires a separately privileged
local broker or separate OS identity. On Linux/macOS the helper uses a private
0600 file, not DPAPI or a platform keychain.

Prompts, selected upload files, stage text and artifacts are not automatically
private from the WorkBuddy agent or its configured model provider. The API-key
boundary does not imply that all media content remains local. H3 providers may
also receive authorized video inputs.

## Operator Commands

Run on AI from `deploy/ai-router`, using the existing Python environment:

```bash
python3 -B integrations/workbuddy/manage.py provision
python3 -B integrations/workbuddy/manage.py verify
python3 -B integrations/workbuddy/manage.py package
python3 -B integrations/workbuddy/manage.py deploy
python3 -B integrations/workbuddy/manage.py doctor
python3 -B integrations/workbuddy/manage.py privacy-audit
```

`provision` creates or verifies only the named account. A pending key-creation
receipt prevents blind duplicate minting if the outcome is unknown. It never
prints the key. `deploy` sends the key through SSH stdin directly to the local
installer, not to a WorkBuddy tool or Tencent endpoint. Do not run provisioning
commands inside WorkBuddy chat.

Updates require the previous public file hashes and refuse mismatched local
changes. Existing credentials are preserved; a different credential requires
explicit operator rotation. Each additional person/device needs its own account
and local provisioning. Do not distribute Ivan's credential directory.

For a public-file-only update, use `manage.py deploy-skill`. It does not send
credentials to the installer or change the Windows credential directory.

`verify_windows.py` is an operator acceptance harness, not part of the Skill
ZIP. Its creation commands invoke real providers. It records stable operation
IDs before submission, so retries do not generate replacement tasks.

## Acceptance Boundary

Evidence is in
`~/.local/state/ai-router-acceptance/20260905-workbuddy-media/`.

The Windows helper is exercised against the real Router, not a local mock.
Desktop WorkBuddy chat/Skill activation is a separate acceptance gate; file
installation and helper success do not prove a model actually selected a Skill.
No existing WorkBuddy conversation or settings were modified, and the desktop
application was not restarted.

Actual client results:

| Operation | Task | Result |
| --- | --- | --- |
| Generate | `img_7a13a4e178d84e7994dc0fdbe4f8588f` | 1254x1254 PNG, green mug, visually inspected |
| Edit | `img_ba7eac631a4342f5822b53c7ba659615` | 1254x1254 PNG, yellow body with preserved handle and red square |
| Video first stage | `vid_4e8aecf9428c469cb68fbc6031a3d196` | Context IR text returned and downloaded; awaiting approval; successors pending |

All three artifacts passed authenticated Windows downloads, SHA-256 validation
and no-clobber checks. Initial submits returned HTTP 202 in approximately
0.7-1.0 seconds including SSH/interpreter startup. Image generation and editing
completed in 37.5 and 43.2 seconds. Replaying original operation IDs returned
the original image tasks.

The combined focused suite passed 367 tests. After final Windows ACL hardening,
the 100 client tests passed again and the actual installer verified the protected
owner/DACL on every private object. Initial grant-only ACL handling left extra
explicit Windows ACEs; it was replaced with an exact user/SYSTEM DACL plus
readback, not accepted as a passing privacy result.

Server-side video stages were previously exercised through all six stage types;
see `media-live-acceptance-2026-09-04.md`. The new-client smoke test stops at the
first video approval boundary. Qwen remains deferred and disabled.

## Skill Visibility Repair

The initial public Skill directory was created from an elevated Windows SSH
session with a protected administrator/OWNER RIGHTS DACL. The desktop WorkBuddy
process is not elevated, so successful SSH reads did not establish desktop
readability. Restarting alone would not remove this permission issue.

The installer now restores inheritance only for the public Skill directory and
its contents, assigns ownership to the Windows user, and verifies user read and
execute access. It does not apply this public-file policy to private credentials.
The public-only repair was deployed without restarting WorkBuddy or changing
credentials.

`windows-desktop-readability.json` records actual reads of all four public files
using the existing non-elevated WorkBuddy process's access token. Their hashes
match the installed package. The private-directory audit and client doctor also
passed again. The client suite now passes 105 tests.

Switch away from the Skills page and return, then search for `siyuan-media`.
The installed app reloads its skill list when that page becomes active. A
successful filesystem access check is still distinct from a user-confirmed
visible card and a completed desktop chat invocation.
