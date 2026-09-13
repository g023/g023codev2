---
name: skill-authoring
description: >
  How to write a g023v2 skill (SKILL.md frontmatter, compact body, when to
  skill_write vs skip). Use when creating or revising a reusable skill.
tags: [skills, authoring]
---

# Writing a g023v2 skill

Skills are optional how-tos for **future sessions on future projects**. They are not session memory and they are not this project's AGENTS.md.

## When to write

Write after you **proved** a procedure (flags that worked, order of steps, a failure to avoid) that another project would hit again.

Skip: one-off paths, secrets, dump of a single repo's files, anything already in the frozen system prompt.

## Shape

`skill_write` fields:

- `slug` — lowercase, digits, hyphens; stable.
- `name` — short label.
- `description` — **the search surface**. Name the trigger phrases and when to load it. If this is vague, the skill will not be found.
- `body` — compact steps. One home per fact. No throat-clearing. No copy of another skill; say "also load `slug`".
- `tags` — optional, a few keywords.

Keep the body well under the size cap. A skill that restates the tool schema is noise.

## When to load

`skill_search` first (empty query lists the catalog). `skill_read` only the hits you will follow. Do not load every skill. Do not paste skill bodies into AGENTS.md.

User skills live in `~/.g023v2/skills/<slug>/SKILL.md` and shadow bundled skills of the same slug. Do not try to overwrite bundled files in-place.
---
