---
name: grok-cli
description: >
  Orchestrate the host grok CLI from g023v2 via run_cli (not run_shell).
  Use when generating images, asking grok to edit files, or running grok -p
  headless. Covers flags, receipts, timeouts, and the --yolo conflict.
tags: [grok, cli, images, run_cli]
---

# grok CLI via run_cli

Use `run_cli`, never `run_shell`, for `grok`. Live output is for the user. History gets a receipt (exit, duration, log path). Do not pump grok dumps through the LLM.

## Headless invocation

```
grok -p "$(cat prompts/cover.txt)" --yolo --output-format plain --no-alt-screen
```

Write the grok prompt to a project file and pass it with `$(cat …)`. A multi-sentence `grok -p '…'` with backslash-escaped quotes breaks `/bin/sh`.

- `--yolo` auto-approves tools. Do **not** also pass `--always-approve` (CLI error: argument used multiple times).
- `--output-format plain` and `--no-alt-screen` keep headless grok from waiting on a TUI.
- Stdin is closed. The process must terminate on its own.
- Default `run_cli` timeout is 600s; image gen often needs `timeout` 900 (max 1800).
- `want_output` stays false unless grok failed and the receipt is not enough. On failure, a short tail is already included; `read_file` the `.g023/scratch/<stamp>/cli/N.log` only if you need more.

## Saving files

Grok's cwd is this project. Tell it the **exact relative path** to write (`images/cover.png`, not "the images folder"). After each call, `find_files` or `list_dir` to confirm the file exists. If grok wrote `images/1.jpg` (or similar) instead, `run_shell` a short `cp` into the required name.

One `run_cli` per illustration or discrete grok task. Do not ask grok to generate a whole book of images in one process.

## Prompting grok for an image

State subject, pose, setting, style, and the save path. For a recurring character, lock identity in every prompt (same species, colors, one distinctive accessory) and restyle from the first image when grok supports editing.

## What not to do

- Do not guess grok flags. This skill is the flag list.
- Do not set `want_output` on success just to "see" the image — `read_image` the saved file if you need pixels.
- Do not pass interactive flags. Do not leave grok waiting for stdin.
---
