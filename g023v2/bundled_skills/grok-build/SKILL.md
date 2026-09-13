---
name: grok-build
description: >
  How Grok Build behaves as a nested agent: headless -p, Imagine image
  tools, skills, cwd, and what to put in the prompt so grok actually saves
  files. Use when the task is "use grok" beyond a single flag reminder.
tags: [grok, grok-build, imagine, agent]
---

# Grok Build as a nested agent

Use this when you are driving **Grok Build** (the `grok` binary) from g023v2. Pair with `grok-cli` for the exact flags.

## What grok can do in one `-p`

Grok is a coding agent with tools: read/edit files in cwd, run commands, search the web, and Imagine (`image_gen` / `image_edit`). A headless `-p` prompt should name the **outcome and path**, not grok's internal tool names unless you need them.

Good: `Draw a red kite over a hill. Save it to images/kite.png in the current working directory. Do not edit other project files.`

Bad: a prompt that only says "make an image" with no path, or that asks grok to rewrite this project's harness.

## Scope the prompt

- Cwd is already the g023v2 project. Say "current working directory".
- "Do not edit other project files" when you only want an image or one new file.
- One outcome per `run_cli`. Do not ask grok to author HTML **and** seven illustrations in one process.
- `--max-turns` can bound a runaway nested agent if the task is tiny.

## Imagine vs code visuals

Grok's image model is for look (characters, scenes, mood). It garbles exact text, numbers, diagrams, and multi-panel grids. For a picture-book page, keep letters **out of the illustration** and put the story in HTML. Do not ask grok to paint the title into the cover art unless the user insisted.

## Skills inside grok

Grok has its own skill system (SKILL.md under `~/.grok/skills` and project `.grok/skills`). You do not load those with g023v2 `skill_read`. If grok needs a procedure, put the constraint in the `-p` prompt.

## Output

Trust the file on disk, not grok's chatter. Confirm with `find_files` / `list_dir`. Use `read_image` if you must see pixels. The `run_cli` receipt is not the image.
---
