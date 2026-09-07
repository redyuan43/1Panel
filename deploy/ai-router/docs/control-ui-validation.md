# Control UI Browser Validation

The console can be tested with headless Chromium; no graphical desktop is needed.
These fixtures exercise the real control API, SQLite persistence, and static UI
with synthetic traces. They do not call production APIs or model endpoints.

## Isolated Preview

From `deploy/ai-router`, using the existing Python test dependencies:

```bash
python3 tests/ui_preview.py \
  --state-dir "$HOME/.local/state/ai-router-ui-preview" \
  --port 14801
```

Open `http://127.0.0.1:14801` on the preview host. The test-only admin key is
`ui-preview-only`. The server binds only to loopback.

The fixture clears inherited `AI_ROUTER_*` environment settings in its own process,
uses an in-memory state store and a separate SQLite/YAML directory, substitutes
synthetic health results, and disables model HTTP clients. Settings and review
writes affect only the preview. Policy drafts, offline replay, activation and
conversation controls are also confined to that directory and in-memory store.
Other management writes, including endpoint reset actions, are rejected. Do not
select a production directory for `--state-dir`.

Stop this preview by its own process ID or terminal interrupt. Do not use broad
`pkill -f uvicorn` patterns.

## Browser Checks

Use an existing Playwright or playwright-core installation and Chromium:

```bash
PLAYWRIGHT_MODULE="/absolute/path/to/playwright-core" \
PLAYWRIGHT_CHROMIUM_EXECUTABLE="/absolute/path/to/chrome" \
UI_TEST_OUTPUT="$HOME/.local/state/ai-router-ui-browser-results" \
node tests/browser_control.cjs
```

The route diagnosis and policy workflow checks can be run separately:

```bash
UI_PREVIEW_URL="http://127.0.0.1:24001" \
PLAYWRIGHT_MODULE="/absolute/path/to/playwright" \
PLAYWRIGHT_CHROMIUM_EXECUTABLE="/usr/bin/google-chrome" \
node tests/browser_route_diagnosis.cjs
```

`PLAYWRIGHT_MODULE` defaults to the Node `playwright` package. The executable
override is optional when that package already has a compatible browser installed.
The test checks the preview marker before making any changes and refuses a
non-loopback hostname. Never point it at a production console.

Covered behavior:

- Simple/detailed Mermaid rendering, cached node labels, and identity/rejection views.
- Chronological per-conversation timeline and selection changes.
- A 105-round timeline, loading earlier rounds and retaining them during refresh.
- Selected evidence node retained during a refresh of the same request.
- Routing and privacy review drafts retained when the other form is submitted.
- Edits made during a pending submission and out-of-order request responses.
- Editable strategy branches and cloud fallback ordering, addition, and removal.
- Protection of the last fallback entry and warnings for unknown endpoints.
- Invalid weights, private URL constraints, and reviewer RPM rejected before PUT.
- Valid fallback and shadow settings saved through the API and restored after reload.
- 1440px, 768px, and 390px layouts, clickable controls, graph fit and zoom.

Results and screenshots are written under `UI_TEST_OUTPUT`, not into Git.
This is UI/API acceptance, not model-quality evaluation or proof of a production
deployment. Run the existing Pytest suite and `node --check` alongside it.
