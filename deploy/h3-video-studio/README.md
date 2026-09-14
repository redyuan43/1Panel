# H3 Video Studio

## AI / Ivan migration candidate

This copy preserves the original H3 page and workflow rather than merging it
into the experimental creative-studio implementation. See
[migration and deployment](docs/ivan-migration.md) and [source provenance](SOURCE.json).
The Edge deployment instructions below are historical, not the AI deployment procedure.

H3 Video Studio is a focused production UI for the MiniMax H3 workflows
validated on the Edge DGX Spark.

## Production flow

0. Use the Skills planner directly below the original prompt: describe the idea,
   inspect automatic selections and their reasons, revise the screenplay in
   natural language, then approve and fill the prompt without leaving the page.
   Approval does not start a video job. The separate `script-studio.html` page
   remains available for detailed editing, history and downloads.
   See [planner capabilities, safety and configuration](docs/script-planner.md).
1. Upload references and submit the original prompt to H3 Context IR.
2. Approve the enhanced prompt.
3. Generate and approve each unlocked stage in the horizontal pipeline.
4. Send the approved 768P result to the official 2K regeneration API.

The local GPU queue is serialized. MiniMax cloud tasks do not hold the local
ComfyUI task lock.

## Scheduled local 768P batches

The `768P Queue` page can collect approved low-resolution projects and run
their local 768P stages sequentially at a one-time date or a daily time.

- Times use `Asia/Shanghai`.
- Daily schedules only run projects explicitly added by the user.
- A running batch has exclusive priority over new local GPU jobs.
- Item-level errors are skipped; ComfyUI, CUDA, disk, or device errors pause
  the batch.
- Completed 768P videos remain awaiting manual approval. Official 2K is never
  started automatically.
- One-time schedules missed during downtime run when the service returns.
  Daily schedules use a six-hour catch-up window.

## Edge deployment

```bash
cd /home/admin/github/h3-video-studio
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p ~/.config/systemd/user
cp deploy/h3-video-studio.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now h3-video-studio.service
```

Open `http://edge.taild500c8.ts.net:8789` from the Tailnet.

The backend reads the MiniMax token from:

```text
/home/admin/.config/minimax/credentials.env
```

The expected line is `MINIMAX_API_KEY=...`. The key is never returned to the
browser.

## Tests

```bash
pytest -q
```
