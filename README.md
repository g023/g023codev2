# g023 Code V2

A local coding agent for your terminal. Point it at a project, give it a task, and it reads, edits, verifies, and keeps going until the work is actually done.

g023 Code V2 talks to **DeepSeek V4.1 Flash** through the Responses API (`deepseek-flash`). The model stays in the cloud. The tools — files, shell, host CLIs, search, images, memory, skills, child agents — run on **your** machine, inside the folder you launched from.

```
╭──────────────────────────────────────────────────────────╮
│  g023v2                                                  │
│  DeepSeek V4.1 Flash coding agent                        │
╰──────────────────────────────────────────────────────────╯

  Type a task, or /goal <text> for a tracked objective.
```

Launch `g023v2` from any project directory. That directory is the project. No extra workspace nesting, no hidden copy of your tree.

> **Platform.** g023 Code V2 is a **Linux** install. It has only been tested as working on **Ubuntu**. Other distros, macOS, and Windows are not a supported target.

---

## Why it exists

Most agent CLIs stream a plan, touch a file, and then ask if you want to continue. This one is built around *finishing*:

- **File tools with backups.** Existing files are snapshotted before they change. A bad edit can be restored.
- **Child agents for bulky work.** Research, exploration, independent edits, and verification go to short-history children so the parent transcript stays small.
- **`/goal` with an evaluator.** Success is withheld until every stated objective shows up in tool evidence, not just in the model's recap.
- **Infinite orchestration.** Leftover issues and recommendations can start a fresh next run from a compact handoff, until the leftovers are empty or you stop it.
- **Searchable skills.** Bundled how-tos plus your own under `~/.g023v2/skills`. The model searches and loads a body only when the task matches, so adding a skill does not rewrite the frozen prompt.
- **`run_cli` for nested programs.** Long-running or chatty host CLIs stream to you, log to disk, and leave only a receipt in model history.
- **Cache-aware on purpose.** The system prompt and tool list are a frozen prefix. Effort, project instructions, skill bodies, and live stamps stay out of that prefix so DeepSeek's prompt cache actually hits.

It is a coding agent, not a chatbot with a `cd`.

---

## Requirements

| Piece | Notes |
|---|---|
| OS | Linux. **Tested on Ubuntu only.** |
| Python | 3.10 or newer (`python3`) |
| DeepSeek API key | One line in `K.dat` at the repo root |
| `requests` | Required (Responses API HTTP) |
| `curl_cffi` or `httpx` | Optional, preferred engines for `fetch_url` (stdlib urllib always works) |
| Pillow | Optional, resizes images to 800px on the long side |

You need a DeepSeek account and an API key from [the DeepSeek platform](https://platform.deepseek.com/). This project is not affiliated with DeepSeek.

---

## Install

```bash
git clone <this-repo> g023-code-v2
cd g023-code-v2
chmod +x install.sh
./install.sh
```

`install.sh` is idempotent. It detects what is missing and only does those pieces:

- Python >= 3.10
- `requests`, plus optional `curl_cffi` / `httpx` / Pillow
- `chmod +x bin/g023v2`
- symlink `~/.local/bin/g023v2` → `bin/g023v2`
- `~/.local/bin` on PATH (current process, plus a marked block in `.bashrc` / `.profile` / `.zshrc` when needed)
- `K.dat` present, non-empty, mode `600`

Check without changing anything:

```bash
./install.sh --check
```

If a new terminal cannot find `g023v2`:

```bash
source ~/.bashrc
```

Non-login interactive bash does not source `~/.profile`, so the installer writes PATH into `.bashrc` as well. Do not assume the symlink is enough.

### API key

Create `K.dat` in the **repo root** (the directory that contains `g023v2/` and `install.sh`), one line, no quotes:

```bash
printf '%s\n' 'YOUR_DEEPSEEK_API_KEY' > K.dat
chmod 600 K.dat
```

Or export `DEEPSEEK_API_KEY` or `G023V2_API_KEY` and re-run `./install.sh`. The installer will write `K.dat` from the environment if the file is absent. It never prints the key and never overwrites an existing file.

`K.dat` is gitignored. Do not commit it. Do not paste it into issues, READMEs, or screenshots.

---

## Quick start

```bash
cd /path/to/your/project
g023v2
```

That opens the interactive REPL on the current directory. Type a task, or start a tracked objective:

```text
/goal Fix the failing tests
- reproduce the failure
- patch the cause, not the assertion
- leave unrelated tests alone
```

One-shot (no REPL):

```bash
g023v2 "what does this repo do?"
g023v2 --once "add a --dry-run flag to the CLI" --effort 80
```

Also valid:

```bash
python3 -m g023v2
python3 /path/to/g023-code-v2/g023_code.py
```

The process working directory is the project unless you pass `--project` and/or `--workspace`.

---

## Using the REPL

On a TTY you get a raw-mode line editor. The prompt prefix is a **local** guess of the next request's size versus the 400k soft context limit, for example `12.4k/400k >`. That number is not sent to the model.

| Key | Effect |
|---|---|
| Arrows | Move the cursor |
| Ctrl+Up / Ctrl+Down | Cycle saved prompts (the in-progress draft is kept) |
| Ctrl+C | Copy the selection, or the whole line if nothing is selected |
| Ctrl+V | Paste |
| Ctrl+Q | Quit after confirmation |
| Enter | Submit |

Ctrl+C **during a running turn** still cancels that turn (SIGINT). At the idle prompt it is copy, not quit. Typed `/quit` or `/exit` shut down immediately.

| Command | Effect |
|---|---|
| `/help` | Commands, infinite orchestration, tool-round cap |
| `/status` | Project, effort, caps, continue policy, live children |
| `/show [topic]` | Inspect the next request **locally** (no API call). Topics: `prompt`, `agents`, `tools`, `schemas`, `extras`, `prefix`, `request`, `effort`, `history` |
| `/goal …` | Tracked objective + evaluator + leftover chain |
| `/infinite` | Help, or `auto` / `prompt` / `N` to set the leftover policy |
| `/effort N` | Next request only (`0` = thinking off, `1–100` = budget) |
| `/children` | Child roster |
| `/team` | Team roster and task board (with `--teams`) |
| `/memory` | Memory snapshot |
| `/history` | History counts + context estimate |
| `/quit` | Shut down |

Submitted prompts are stored under the project as `.g023/prompts/{n}.txt`. Status commands are not.

---

## CLI flags

| Flag | Purpose |
|---|---|
| `PROMPT` | Same as `--once`: one prompt, then exit |
| `--project PATH` | Project directory (default: cwd) |
| `--workspace PATH` | With a relative `--project`, root is `workspace/project`. Alone, this path is the root. An absolute `--project` plus `--workspace` is rejected. |
| `--teams` | Captain-led team with a shared task board, instead of parent-child |
| `--fresh` | Default. Empty conversation. Deletes this project's session log. Does **not** wipe memory or the URL cache. |
| `--resume` | Replay `session.jsonl` (history, compact markers, last effort) |
| `--once TEXT` | One prompt, then exit |
| `--image PATH_OR_URL` | Attach an image to the first user or `/goal` turn only |
| `--effort N` | First request of this process only (`0–100`). Later rounds follow the model's own next-round control. |
| `--infinite auto\|prompt\|N` | Leftover-driven chain after a finished task. `--goal-continue` is the same flag. |

Examples:

```bash
g023v2 --resume
g023v2 --project ./backend --effort 40
g023v2 --once "/goal ship the migration" --infinite prompt
g023v2 --image screenshot.png "what is broken in this UI?"
```

---

## What the agent can do

### File tools (path-jailed to the project)

`read_file`, `read_files`, `write_file`, `edit_file`, `apply_edits`, `replace_all`, `replace_in_files`, `restore_file`, `list_dir`, `find_files`, `grep`.

Writes of existing files copy the current bytes to `.g023/backups/<stamp>/` first. `restore_file` copies the last snapshot back. Brand-new files have no snapshot until they are overwritten. Live files are chmod `0644` after replace so a checkout or web server can actually read them.

Paths that escape the project root, or that land in `.g023/backups`, `.g023/logs`, `.g023/usage`, or `.g023/prompts`, are refused. `.g023/scratch/` is writable working space.

### Shell and host CLIs

`run_shell` is **unsandboxed** host execution with the project as cwd. Timeout (default 60s, max 300s), process-group kill, and output caps bound it. There is no container or seccomp jail. Treat it as "the model can run a command as you."

`run_cli` is the same unsandboxed host, aimed at long-running or chatty programs (another coding CLI, a test suite, image generation). Stdout and stderr are merged and **relayed live** to you, written under `.g023/scratch/<stamp>/cli/`, and the model gets a short receipt (exit, duration, log path) instead of the dump. Default timeout 600s, max 1800s. Ctrl+C during a turn kills the process group. Prefer this over `run_shell` when the program would otherwise flood conversation history.

### Skills (search-on-demand how-tos)

Skills are optional global procedures. They are **not** dumped into the system prompt and they are **not** this project's `AGENTS.md`.

| Tool | Effect |
|---|---|
| `skill_search` | Catalog hits by keyword. Empty query lists the catalog. |
| `skill_read` | Load one skill body by slug. |
| `skill_write` | Save a user skill under `~/.g023v2/skills/<slug>/SKILL.md`. Does not overwrite bundled files. |

Shipped bundled slugs: `skill-authoring`. A user skill of the same slug shadows the bundled one. Workers may search, read, and write (these are base tools).

### Web and memory

- `web_search` — server-side search (DeepSeek native tool)
- `fetch_url` — GET one `http`/`https` URL as readable text, with a global on-disk cache (`auto` / `cache` / `fresh`)
- `memory_write` / `memory_read` / `memory_list` — facts that should survive compaction, keyed per project

### Vision

`--image` on the first turn, or `read_image` mid-session. JPEG / PNG / GIF / WebP. With Pillow, images are constrain-resized to 800px on the long side. Without Pillow, a known image larger than that is refused rather than sent full-size.

### Delegation

Default mode is **parent-child**. The parent keeps the plan and integration. Children handle bulky slices (`research`, `explore`, `implement`, `verify`, or a depth-1 `lead` that may spawn workers). `plan_work` plus `join_children` is the usual loop. Overlapping `files` on plan items are held so two workers do not edit the same path.

`--teams` swaps that for a persistent roster and a shared task board.

### `/goal` and leftovers

`/goal` prepends stable instructions (not a mutated system prompt), gives the model a compaction tool, and runs an evaluator after each inner turn. Reciting the objective list is not evidence. Tool output is.

When a task finishes, leftover issues and recommendations can start a **new** orchestration from a compact handoff:

| Policy | Behavior |
|---|---|
| `auto` | Start the next run without asking. Unattended `--once` still brakes at 8 chained runs. In the REPL, you are the brake (picker, burst, Ctrl+C). |
| `prompt` | Ask before every subsequent run. |
| `N` | Run N automatic subsequent runs, then ask (or stop unattended). |

Ordinary `--once` does not chain leftovers unless you pass `--infinite` / `--goal-continue`.

---

## Project instructions (`AGENTS.md`)

If the active project root contains a non-empty `AGENTS.md`, that file is injected every tool round as a `[PROJECT AGENTS.md]` input item. Nested `AGENTS.md` files in subfolders are ignored.

This is how you tell the agent how *this* repo works — language, test command, files it must not touch — without forking the frozen system prompt. Changing projects changes that item, not the cacheable prefix.

---

## Where data lives

Nothing in this table is your API key. The key stays in `K.dat` at the **g023 Code V2 repo root**, never in the project you are editing.

| Path | What |
|---|---|
| `<repo>/K.dat` | API key, mode `600` |
| `<project>/.g023/logs/` | `/goal` mission log |
| `<project>/.g023/usage/` | Token / tool / file usage table |
| `<project>/.g023/scratch/` | Working folder, omitted tool-result vault, and `run_cli` logs under `cli/` |
| `<project>/.g023/backups/` | Per-stamp file-tool snapshots |
| `<project>/.g023/prompts/` | Saved user prompts |
| `g023v2/bundled_skills/` | Shipped skill how-tos (`SKILL.md` per slug) |
| `~/.g023v2/skills/` | User-written skills, shared across every project |
| temp `deepseek_harness_memory/` | Per-project `memory.json` + `session.jsonl`, plus a global URL cache |

`.g023` is created lazily and gitignored from the project's point of view (the harness writes a `.g023/.gitignore`). Launch defaults to a **fresh** conversation: the session log is deleted, memory keys and the URL cache are not.

---

## Limits worth knowing

| Limit | Value |
|---|---|
| Model | `deepseek-flash` only |
| Tool-using rounds per turn | 64, then one wrap-up that does not dispatch tools. That wrap-up is **not** a cancel. |
| `/goal` evaluator turns | 12 |
| Unattended leftover chain | 8 |
| Concurrent children | 12 |
| Child depth | parent → optional lead → workers |
| Soft context | 400,000 tokens (compaction around 80%) |
| Reasoning effort | `0` = off, `1–100` = thinking budget. Default first request is 60. |
| `run_cli` timeout | 600s default, 1800s max |
| Skill body | 24,000 chars |

Hitting the tool-round cap ends that inner turn after wrap-up. Later `/goal` turns and leftover runs can still use tools.

---

## Safety

- File tools cannot leave the project root. `run_shell` and `run_cli` can. Review destructive prompts the way you would review a script you were about to run as yourself.
- The API key is never printed in the REPL banner, `/show`, or installer output. Ready vs missing is enough.
- Do not run this as root. Do not point `--project` at a tree you cannot afford to change without a VCS.
- `fetch_url` does not execute JavaScript. Client-rendered app shells may return little text.

---

## Troubleshooting

**`g023v2: command not found`**  
`~/.local/bin` is not on PATH in this shell. `source ~/.bashrc`, or call `bin/g023v2` by path. `./install.sh --check` reports the gap.

**`missing API key file`**  
`K.dat` belongs next to `g023v2/`, not in the project you are editing and not next to the PATH symlink.

**Thinking will not turn off at `--effort 0`**  
On this API, integer `0` still thinks. The harness maps internal `0` to `reasoning.effort: "none"`. If you see thinking anyway, check `/show effort`.

**Images refused**  
Install Pillow (`./install.sh` will try) or shrink the image so the long side is at most 800px.

**`--help` works, a real prompt 401s**  
The key file is present but wrong. Replace the line in `K.dat`. The installer will not overwrite it.

---

## What this repo is

This tree is the **runtime**: the `g023v2` package, the `bin/g023v2` launcher, `g023_code.py`, and `install.sh`. It is meant to be cloned, installed, and used.

It is **not** a general-purpose SDK for other models. The wire format, cache layout, and thinking control are built for DeepSeek V4.1 Flash's Responses API.

---

## License

Use and redistribute at your own risk. Bring your own DeepSeek API key and stay inside DeepSeek's terms.
