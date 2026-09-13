---
name: picture-book-with-grok
description: >
  Write a children's picture book (index.html + images/) by orchestrating
  grok -p for each illustration. Use when the user wants a picture book,
  storybook, or illustrated pages whose art comes from the grok CLI.
tags: [book, grok, images, children, html]
---

# Picture book with grok illustrations

Load `grok-cli` as well. This skill is the book shape; that one is the CLI.

## Deliverable

- Original story, ages 4–7. No copyrighted characters.
- About 6–8 story pages plus a cover.
- `index.html` that reads as a picture book: title, then each page with its illustration and a short paragraph.
- Images under `images/` with relative `src` (`images/cover.png`, `images/page-1.png`, …).
- Readable on a phone: fluid images, comfortable type, no tiny gray text.

## Order of work

1. Write the story (page list with one beat and one illustration brief each).
2. Create `images/` .
3. Generate the **cover first**, then pages in order. One `run_cli` per image.
4. After each grok call, confirm the file exists before the next image.
5. Write `index.html` referencing the files that actually landed (png or jpg).
6. Open or re-read the HTML and check every `src`.

## Character lock

Pick one distinctive accessory (scarf, hat, backpack) and a short style lock. Repeat both in **every** grok prompt:

```
children's picture book watercolor on warm paper, soft edges, friendly animal
characters, consistent character design, no text or letters in the image
```

Ask grok to save to the exact path, e.g. `images/page-3.png`. Timeout 900+. Put each illustration prompt in `prompts/NAME.txt` and invoke grok as in `grok-cli` (`$(cat …)`), not an inline quoted string.

## HTML

One file. Cover block, then `.page` blocks (image + 1–2 short paragraphs + page number). No nav chrome, no "click to start". Semantic `alt` text that describes the scene.

## Failures

If grok writes a different filename, copy it. If an image is missing, retry that one prompt once with a tighter save-path sentence; then continue the book rather than stalling the whole set.
---
