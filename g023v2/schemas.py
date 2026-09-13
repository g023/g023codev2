"""Tool schemas. Built once per session via build_tool_schemas()."""

from __future__ import annotations

import copy
from typing import Any

from g023v2.plan import LEAD_TOOL_NAMES

BASE_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "read_file",
        "description": "Read a text file. Returns content with line numbers. Binary files are rejected.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the project directory."},
                "offset": {"type": "integer", "description": "First line to return, 1-based. Default 1."},
                "limit": {"type": "integer", "description": "Maximum lines to return. Default 400."},
            },
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "read_files",
        "description": (
            "Read several text files in one call (same line-number format as read_file). "
            "Path-jailed. Caps how many paths you may pass. Prefer this over many read_file calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Project-relative files to read, in order.",
                },
                "offset": {"type": "integer", "description": "First line to return in each file, 1-based. Default 1."},
                "limit": {"type": "integer", "description": "Maximum lines per file. Default 400."},
            },
            "required": ["paths"],
        },
    },
    {
        "type": "function",
        "name": "write_file",
        "description": (
            "Write a complete text file. Overwrites if it exists. Creates parent directories. "
            "If the file already exists, its previous contents are backed up first and can be "
            "recovered with restore_file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the project directory."},
                "content": {"type": "string", "description": "Full file content."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "type": "function",
        "name": "edit_file",
        "description": (
            "Replace the first exact occurrence of old_string with new_string in a file. "
            "old_string must appear exactly once; if it appears more than once, use replace_all. "
            "The previous contents are backed up first and can be recovered with restore_file. "
            "For several unique hunks in one file, use apply_edits."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the project directory."},
                "old_string": {"type": "string", "description": "Exact text to replace. Must be unique."},
                "new_string": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "type": "function",
        "name": "apply_edits",
        "description": (
            "Apply several unique exact replacements in one file, in order, with one backup. "
            "Each old_string must appear exactly once in the text as of that step. "
            "Prefer this over many edit_file calls. restore_file recovers the pre-edit snapshot."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the project directory."},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                        },
                        "required": ["old_string", "new_string"],
                    },
                    "description": "Ordered unique replacements.",
                },
            },
            "required": ["path", "edits"],
        },
    },
    {
        "type": "function",
        "name": "replace_all",
        "description": (
            "Replace every occurrence of old_string with new_string in one project file. "
            "Prefer this over run_shell sed. Backs up first; restore_file recovers. "
            "For a single unique occurrence, use edit_file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the project directory."},
                "old_string": {"type": "string", "description": "Exact text to replace in every occurrence."},
                "new_string": {"type": "string", "description": "Replacement text."},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "type": "function",
        "name": "replace_in_files",
        "description": (
            "Replace every occurrence of old_string with new_string in every matching "
            "project text file under path. glob is a name/path glob (same as find_files). "
            "Path-jailed; skips vendor/backup dirs and binary files. Backs up each changed "
            "file; restore_file recovers per path. Prefer this over run_shell sed or "
            "many replace_all calls. Reports how many occurrences and files changed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "glob": {"type": "string", "description": "Filename glob, e.g. '*.py' or 'src/**/*.ts'."},
                "old_string": {"type": "string", "description": "Exact text to replace in every occurrence."},
                "new_string": {"type": "string", "description": "Replacement text."},
                "path": {"type": "string", "description": "Relative directory or file to search under. Default '.'."},
                "max_files": {
                    "type": "integer",
                    "description": "Maximum files to mutate. Default 200.",
                },
            },
            "required": ["glob", "old_string", "new_string"],
        },
    },
    {
        "type": "function",
        "name": "restore_file",
        "description": (
            "Restore a file from the last backup taken before write_file or edit_file. "
            "Use after a bad edit. New files that have never been overwritten have no backup."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path inside the project directory."},
            },
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "list_dir",
        "description": "List files and directories under a project path.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path. Default '.'."},
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "find_files",
        "description": (
            "Recursively find project files by name glob (e.g. '*.py', 'src/**/*.ts'). "
            "Path-jailed; skips vendor/cache/backup dirs. Prefer this over run_shell find or ls -R. "
            "Caps results and marks truncation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "glob": {"type": "string", "description": "Filename glob, e.g. '*.py' or 'src/**/*.ts'."},
                "path": {"type": "string", "description": "Relative directory or file to search under. Default '.'."},
                "max_results": {"type": "integer", "description": "Maximum matching paths. Default 500."},
            },
            "required": ["glob"],
        },
    },
    {
        "type": "function",
        "name": "grep",
        "description": "Search file contents with a regular expression. Skips common vendor and cache directories.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression pattern."},
                "path": {"type": "string", "description": "Relative directory or file. Default '.'."},
                "glob": {"type": "string", "description": "Optional glob filter, e.g. '*.py'."},
                "max_results": {"type": "integer", "description": "Maximum matching lines. Default 100."},
            },
            "required": ["pattern"],
        },
    },
    {
        "type": "function",
        "name": "run_shell",
        "description": (
            "Run a non-interactive shell command inside the project directory. "
            "Do not use this to find files (find_files), replace every occurrence in a file "
            "(replace_all), replace across files (replace_in_files), list memory keys "
            "(memory_list), fetch a URL (fetch_url), or read an image (read_image). "
            "Do not use this to run another agentic CLI (grok -p, other harnesses); "
            "use run_cli so live output goes to the user and the dump stays out of history. "
            "Large stdout/stderr is shaped to a [TOOL RECEIPT] (test failures + head/tail); "
            "the omitted span is written under .g023/scratch/<stamp>/vault/ for read_file."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command. Must terminate without stdin."},
                "timeout": {"type": "integer", "description": "Seconds before the command is killed. Default 60."},
            },
            "required": ["command"],
        },
    },
    {
        "type": "function",
        "name": "run_cli",
        "description": (
            "Run a host CLI or another agentic harness in the project directory "
            "(for example grok -p '...'). Live stdout/stderr is relayed to the user "
            "and written to a log under .g023/scratch/<stamp>/cli/. The tool result "
            "is a short receipt (exit, duration, log path), not the program dump. "
            "Set want_output true, or read_file the log, only when you need those bytes. "
            "Prefer this over run_shell for long-running or chatty programs. "
            "Unsandboxed. The process has no stdin; it must terminate on its own. "
            "Default timeout 600s, max 1800s."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command to run in the project directory. "
                        "Must terminate without stdin. Example: grok -p 'draw a cat' --yolo"
                    ),
                },
                "timeout": {
                    "type": "integer",
                    "description": "Seconds before the process group is killed. Default 600, max 1800.",
                },
                "want_output": {
                    "type": "boolean",
                    "description": (
                        "If true, include a capped tail of the log in the result. "
                        "Default false: receipt only. A short tail is included on failure."
                    ),
                },
            },
            "required": ["command"],
        },
    },
    {
        "type": "function",
        "name": "memory_write",
        "description": "Persist a key-value fact in the session memory store.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short identifier."},
                "value": {"type": "string", "description": "Concrete fact or decision."},
            },
            "required": ["key", "value"],
        },
    },
    {
        "type": "function",
        "name": "memory_read",
        "description": "Read a value from the session memory store.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key previously written with memory_write."},
            },
            "required": ["key"],
        },
    },
    {
        "type": "function",
        "name": "memory_list",
        "description": (
            "List keys currently stored in session memory. Does not return values; "
            "use memory_read for a value. Empty store returns a distinct empty result."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "skill_search",
        "description": (
            "Search the global skill store (bundled + user how-tos, every project). "
            "Returns matching slug/description rows, not full bodies. "
            "Empty query lists the catalog. Call this when a task may match a "
            "reusable procedure (grok CLI, picture books, public web, …). "
            "Then skill_read only the hits you will follow. Skills are optional; "
            "do not load ones you do not need."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to match against slug, name, description, and tags. Empty lists the catalog.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum hits to return. Default 8.",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "skill_read",
        "description": (
            "Load the full body of one skill by slug (from skill_search). "
            "Do not read skills you will not follow."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Skill slug, e.g. grok-cli."},
            },
            "required": ["slug"],
        },
    },
    {
        "type": "function",
        "name": "skill_write",
        "description": (
            "Create or update a user skill in the global store (~/.g023v2/skills) "
            "so future sessions on future projects can skill_search it. "
            "Write only a durable how-to you have proven, not a project dump "
            "or a one-off. Description is the search surface. User slugs shadow "
            "bundled skills; this does not overwrite bundled files in-place."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "slug": {
                    "type": "string",
                    "description": "Lowercase letters, digits, hyphens; 1-63 chars.",
                },
                "name": {"type": "string", "description": "Short label. Defaults to slug."},
                "description": {
                    "type": "string",
                    "description": "When to use this skill. This is what skill_search matches.",
                },
                "body": {
                    "type": "string",
                    "description": "Compact markdown how-to (no need to wrap in YAML frontmatter).",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional keywords for search.",
                },
            },
            "required": ["slug", "description", "body"],
        },
    },
    {
        "type": "function",
        "name": "fetch_url",
        "description": (
            "Fetch one http or https URL and return readable text (HTML is stripped to prose). "
            "A global cache stores every successful fetch across projects. "
            "cache_mode=auto uses a copy younger than max_age; cache uses only the store; "
            "fresh always hits the network. Non-http(s) is rejected. "
            "Prefer this over run_shell curl/wget. web_search is search-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http or https URL to GET."},
                "timeout": {
                    "type": "integer",
                    "description": "Seconds before the GET is aborted. Default 15, max 30.",
                },
                "cache_mode": {
                    "type": "string",
                    "enum": ["auto", "fresh", "cache"],
                    "description": (
                        "auto = use a cached copy younger than max_age, else fetch; "
                        "fresh = always hit the network; "
                        "cache = only the store, never the network."
                    ),
                },
                "max_age": {
                    "type": "integer",
                    "description": "In auto mode, oldest acceptable cached copy in seconds (default 3600).",
                },
                "extract": {
                    "type": "string",
                    "enum": ["text", "markdown", "links", "raw"],
                    "description": "How to render the page. raw is the stored body. Default text.",
                },
                "max_chars": {
                    "type": "integer",
                    "description": (
                        "Truncate returned content to this many characters (default 20000). "
                        "The full stored body stays in the cache; raise max_chars with "
                        "cache_mode=cache to read more without another network request."
                    ),
                },
            },
            "required": ["url"],
        },
    },
    {
        "type": "function",
        "name": "read_image",
        "description": (
            "Ingest a project image (JPEG, PNG, GIF, or WebP) as a vision input_image "
            "so the next round can see the pixels. Local path only, path-jailed. "
            "Do not use read_file on images (that rejects binary). "
            "Do not dump image bytes as text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to an image inside the project directory."},
            },
            "required": ["path"],
        },
    },
    {"type": "web_search"},
]


GOAL_COMPACT_TOOL_NAME = "compact_goal_conversation"

GOAL_COMPACT_TOOL: dict[str, Any] = {
    "type": "function",
    "name": GOAL_COMPACT_TOOL_NAME,
    "description": (
        "Replace this /goal conversation with one compact record of the goal, "
        "what we've done, what is to be done, and what is to be done next. "
        "Call only at a safe point and only when the conversation is getting long. "
        "Pre-compact turns, tool calls, and reasoning items are dropped. "
        "Available only during a /goal run."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "The stated goal still being pursued.",
            },
            "done": {
                "type": "string",
                "description": "What we've done so far.",
            },
            "remaining": {
                "type": "string",
                "description": "What is to be done (remaining work).",
            },
            "next": {
                "type": "string",
                "description": "What is to be done next (the immediate next step).",
            },
        },
        "required": ["goal", "done", "remaining", "next"],
    },
}


PARENT_CHILD_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "spawn_child",
        "description": (
            "Create a one-shot child agent for a bounded task. The child runs "
            "in its own thread with an isolated short history. Default fresh=true "
            "(do not copy this conversation; that re-bills bulky context). "
            "role selects the default effort and, for lead, a one-level spawn right."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Short lowercase identifier for the child."},
                "task": {"type": "string", "description": "Clear objective for the child."},
                "effort": {"type": "integer", "description": "Reasoning effort for the child. 0 = thinking off; 1-100 = thinking budget. Omit to use the role default."},
                "context": {"type": "string", "description": "Optional supplemental context passed to the child."},
                "fresh": {"type": "boolean", "description": "If false, the child inherits the parent's history up to the last clean turn boundary. Default true. Prefer true."},
                "role": {
                    "type": "string",
                    "enum": ["research", "explore", "implement", "verify", "lead"],
                    "description": (
                        "research=search/fetch (effort 25), explore=grep/read (20), "
                        "implement=edits (40), verify=tests (25), lead=fan-out workers (40). "
                        "A lead may spawn workers, not other leads. Default: generic worker."
                    ),
                },
            },
            "required": ["name", "task"],
        },
    },
    {
        "type": "function",
        "name": "send_message",
        "description": "Queue a message for a child. Delivered between the child's tool rounds.",
        "parameters": {
            "type": "object",
            "properties": {
                "child": {"type": "string", "description": "Child name."},
                "message": {"type": "string", "description": "Message content."},
                "kind": {"type": "string", "enum": ["supplement", "redirect", "question"], "description": "Default 'supplement'."},
            },
            "required": ["child", "message"],
        },
    },
    {
        "type": "function",
        "name": "edit_message",
        "description": "Edit a queued message before it is delivered. Only pending messages can be edited.",
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Message id returned by send_message."},
                "new_content": {"type": "string", "description": "Replacement content."},
            },
            "required": ["message_id", "new_content"],
        },
    },
    {
        "type": "function",
        "name": "delete_message",
        "description": "Delete a queued message before it is delivered.",
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Message id returned by send_message."},
            },
            "required": ["message_id"],
        },
    },
    {
        "type": "function",
        "name": "interject",
        "description": "Inject a high-priority instruction the child processes before its next normal step.",
        "parameters": {
            "type": "object",
            "properties": {
                "child": {"type": "string", "description": "Child name."},
                "message": {"type": "string", "description": "High-priority instruction."},
            },
            "required": ["child", "message"],
        },
    },
    {
        "type": "function",
        "name": "stop_child",
        "description": (
            "Cancel a child's current turn. Persistent team members stay available. "
            "Stopping a one-shot child ends it and frees the name for respawn."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "child": {"type": "string", "description": "Child name."},
            },
            "required": ["child"],
        },
    },
    {
        "type": "function",
        "name": "list_children",
        "description": "List all child agents, their status, and their latest summary.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "get_child_result",
        "description": (
            "Wait for a child's completion (up to a finite timeout) and return "
            "its compact final output (truncated if long). Returns a still-running "
            "notice if the timeout elapses first. Prefer join_children to wait on several."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "child": {"type": "string", "description": "Child name."},
                "timeout": {
                    "type": "number",
                    "description": "Seconds to wait. Default 120. Clamped to a finite maximum.",
                },
            },
            "required": ["child"],
        },
    },
    {
        "type": "function",
        "name": "plan_work",
        "description": (
            "Maintain the session work plan (todos that can spawn children). "
            "action=add|update|list|complete|spawn. spawn starts a child for every "
            "ready pending item (dependencies complete) in parallel, but holds an "
            "item whose files overlap a running sibling so two workers do not "
            "edit the same path. Join reports [PLAN] ready/open; do not re-complete "
            "items join already closed. Do not treat an open plan as finished work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "update", "list", "complete", "spawn"],
                    "description": "add a todo, update fields, list, mark complete, or spawn ready items.",
                },
                "id": {"type": "string", "description": "Plan item id (p1, p2, …). Required for update/complete."},
                "title": {"type": "string", "description": "Short todo title. Required for add."},
                "details": {"type": "string", "description": "Full instructions for the worker."},
                "role": {
                    "type": "string",
                    "enum": ["research", "explore", "implement", "verify", "lead"],
                    "description": "Worker role. lead is parent-only; a lead cannot spawn another lead.",
                },
                "effort": {"type": "integer", "description": "Override role default effort."},
                "depends_on": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Plan ids that must complete before this item can spawn.",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File paths this item will touch or inspect.",
                },
                "result": {"type": "string", "description": "Completion notes when action=complete."},
            },
            "required": ["action"],
        },
    },
    {
        "type": "function",
        "name": "join_children",
        "description": (
            "Wait for children in parallel and return their compact results "
            "plus a [PLAN] snapshot (ready to spawn / still open / empty). "
            "Join auto-completes plan items owned by those children; do not "
            "plan_work complete them again. Spawn next ready items after join. "
            "Omit children to wait on every live/uncollected child you own "
            "(parent: all; lead: workers you spawned). Use this before claiming done."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "children": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Child names. Empty or omitted = all you own.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Seconds to wait. Default 600. Clamped to a finite maximum.",
                },
            },
            "required": [],
        },
    },
]


TEAM_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "team_create_member",
        "description": "Create a persistent named teammate with a role and reasoning effort.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique lowercase-kebab-case name."},
                "role": {"type": "string", "description": "Short role description, e.g. 'reviewer', 'builder'."},
                "effort": {"type": "integer", "description": "Reasoning effort. 0 = thinking off; 1-100 = thinking budget. Default 40."},
            },
            "required": ["name", "role"],
        },
    },
    {
        "type": "function",
        "name": "team_create_task",
        "description": "Add a task to the shared board with optional dependencies and file hints.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short task title."},
                "details": {"type": "string", "description": "Full task description."},
                "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Task ids this task depends on."},
                "files": {"type": "array", "items": {"type": "string"}, "description": "File paths this task will touch."},
            },
            "required": ["title", "details"],
        },
    },
    {
        "type": "function",
        "name": "team_claim_task",
        "description": "Claim a ready task as a member. Dispatches the task to that member's inbox.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id."},
                "member": {"type": "string", "description": "Member name."},
            },
            "required": ["task_id", "member"],
        },
    },
    {
        "type": "function",
        "name": "team_complete_task",
        "description": "Mark a task complete and record the result. Usually called by the member itself.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task id."},
                "member": {"type": "string", "description": "Member name."},
                "result": {"type": "string", "description": "Result summary."},
            },
            "required": ["task_id", "member", "result"],
        },
    },
    {
        "type": "function",
        "name": "team_send_message",
        "description": "Send a message from one member to another, or to the captain.",
        "parameters": {
            "type": "object",
            "properties": {
                "sender": {"type": "string", "description": "Sender name, or 'captain'."},
                "recipient": {"type": "string", "description": "Recipient name, or 'captain'."},
                "message": {"type": "string", "description": "Message content."},
            },
            "required": ["sender", "recipient", "message"],
        },
    },
    {
        "type": "function",
        "name": "team_status",
        "description": "Show the current roster and task board state.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    {
        "type": "function",
        "name": "team_interrupt",
        "description": "Stop a member's current turn. Queued messages and task ownership are preserved.",
        "parameters": {
            "type": "object",
            "properties": {
                "member": {"type": "string", "description": "Member name."},
            },
            "required": ["member"],
        },
    },
    {
        "type": "function",
        "name": "team_list_tasks",
        "description": "List all tasks on the shared board with their status and owner.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
]


def _schema_names(schemas: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for schema in schemas:
        if not isinstance(schema, dict):
            continue
        name = schema.get("name")
        if name:
            names.add(str(name))
        elif schema.get("type") == "web_search":
            names.add("web_search")
    return names


def dispatch_allowlist(
    teams_enabled: bool = False,
    *,
    goal_mode: bool = False,
    delegate: bool = False,
    lead: bool = False,
) -> frozenset[str]:
    """Names this agent/mode may execute.

    The request `tools` array is a session-lifetime superset (see
    `build_tool_schemas`). Seeing a name there is not permission to run it.
    Worker children and team members get only BASE names. A depth-0 lead
    also gets the lead spawn/join/plan subset (not nested leads, not
    compact). `compact_goal_conversation` is executable only while
    `goal_mode` is true (and dispatch still checks that a `/goal` run is
    active).
    """
    names = _schema_names(BASE_TOOL_SCHEMAS)
    if delegate:
        if lead:
            names.update(LEAD_TOOL_NAMES)
        return frozenset(names)
    extra = TEAM_TOOLS if teams_enabled else PARENT_CHILD_TOOLS
    names.update(_schema_names(extra))
    if goal_mode:
        names.add(GOAL_COMPACT_TOOL_NAME)
    return frozenset(names)


def build_tool_schemas(
    teams_enabled: bool,
    *,
    goal_mode: bool = False,
) -> list[dict[str, Any]]:
    """Byte-stable session-lifetime `tools` list.

    Always the superset: BASE + parent-child or team extras +
    `compact_goal_conversation`. `goal_mode` is kept for callers and does
    not fork the list; dispatch uses `dispatch_allowlist` instead.
    Each call deep-copies so mutating a returned dict cannot stick.
    """
    del goal_mode  # list does not fork; execution is jailable separately
    schemas = copy.deepcopy(BASE_TOOL_SCHEMAS)
    extras = TEAM_TOOLS if teams_enabled else PARENT_CHILD_TOOLS
    schemas.extend(copy.deepcopy(extras))
    schemas.append(copy.deepcopy(GOAL_COMPACT_TOOL))
    return schemas
