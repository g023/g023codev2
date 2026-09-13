"""Global searchable skills. Import-safe: does not mkdir."""

from __future__ import annotations

import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from g023v2.constants import (
    SKILL_BODY_MAX_CHARS,
    SKILL_CATALOG_MAX,
    SKILL_SEARCH_DEFAULT_LIMIT,
    SKILL_SEARCH_MAX_LIMIT,
    SKILLS_BUNDLED_DIR,
    SKILLS_USER_DIR,
)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_FM_OPEN = re.compile(r"^---\s*\n", re.MULTILINE)


@dataclass(frozen=True)
class SkillMeta:
    slug: str
    name: str
    description: str
    tags: tuple[str, ...] = ()
    source: str = "bundled"  # bundled | user
    path: Path = field(default_factory=Path)


def valid_slug(slug: str) -> bool:
    return bool(slug) and SLUG_RE.fullmatch(slug) is not None


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse a YAML-subset frontmatter block. Unknown keys are kept as strings."""
    raw = (text or "").replace("\r\n", "\n")
    if not raw.startswith("---"):
        return {}, raw
    rest = raw[3:]
    if rest.startswith("\n"):
        rest = rest[1:]
    end = rest.find("\n---")
    if end < 0:
        return {}, raw
    fm_text = rest[:end]
    body = rest[end + 4 :]
    if body.startswith("\n"):
        body = body[1:]
    return _parse_yaml_subset(fm_text), body


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_flow_list(value: str) -> list[str]:
    inner = value.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    items: list[str] = []
    for part in inner.split(","):
        item = _unquote(part)
        if item:
            items.append(item)
    return items


def _parse_yaml_subset(fm_text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    lines = fm_text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, raw_val = stripped.partition(":")
        key = key.strip()
        if not key:
            continue
        raw_val = raw_val.strip()
        if raw_val in (">", "|"):
            folded = raw_val == ">"
            collected: list[str] = []
            while i < n:
                nxt = lines[i]
                if nxt.strip() and not nxt.startswith((" ", "\t")) and ":" in nxt:
                    break
                if nxt.startswith("- ") and not nxt.startswith((" ", "\t")):
                    break
                i += 1
                collected.append(nxt.strip() if folded else nxt[2:] if nxt.startswith("  ") else nxt)
            if folded:
                data[key] = " ".join(p for p in collected if p)
            else:
                data[key] = "\n".join(collected).strip("\n")
            continue
        if raw_val.startswith("[") and raw_val.endswith("]"):
            data[key] = _parse_flow_list(raw_val)
            continue
        if raw_val == "":
            items: list[str] = []
            while i < n:
                nxt = lines[i]
                if nxt.strip().startswith("- "):
                    items.append(_unquote(nxt.strip()[2:]))
                    i += 1
                    continue
                break
            if items:
                data[key] = items
            else:
                data[key] = ""
            continue
        data[key] = _unquote(raw_val)
    return data


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _as_tags(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        parts = [p.strip().lower() for p in value.replace(";", ",").split(",")]
        return tuple(p for p in parts if p)
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            s = str(item).strip().lower()
            if s:
                out.append(s)
        return tuple(out)
    return ()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _score(meta: SkillMeta, query: str, tokens: list[str]) -> int:
    if not query and not tokens:
        return 1
    score = 0
    slug = meta.slug.lower()
    name = meta.name.lower()
    desc = meta.description.lower()
    tags = {t.lower() for t in meta.tags}
    q = query.lower().strip()
    if q:
        if q == slug or q == name:
            score += 80
        elif q in slug or q in name:
            score += 40
        if q in desc:
            score += 20
    for tok in tokens:
        if tok == slug or tok == name.replace(" ", "-"):
            score += 50
        elif tok in slug or tok in name:
            score += 18
        if tok in desc:
            score += 8
        if tok in tags:
            score += 16
    return score


def render_skill_markdown(
    *,
    name: str,
    description: str,
    body: str,
    tags: Iterable[str] = (),
) -> str:
    tag_list = [t for t in _as_tags(list(tags) if not isinstance(tags, str) else tags)]
    lines = ["---", f"name: {name}"]
    desc = (description or "").strip()
    if "\n" in desc or len(desc) > 80:
        lines.append("description: >")
        for para in desc.split("\n"):
            lines.append(f"  {para.strip()}")
    else:
        lines.append(f"description: {desc}")
    if tag_list:
        inner = ", ".join(tag_list)
        lines.append(f"tags: [{inner}]")
    lines.append("---")
    lines.append("")
    body = (body or "").strip()
    if body:
        lines.append(body)
        if not body.endswith("\n"):
            lines.append("")
    return "\n".join(lines)


class SkillStore:
    """Bundled + user skill directories. User slugs shadow bundled slugs."""

    def __init__(
        self,
        bundled_dir: Path | None = None,
        user_dir: Path | None = None,
    ):
        self.bundled_dir = Path(bundled_dir) if bundled_dir else SKILLS_BUNDLED_DIR
        self.user_dir = Path(user_dir) if user_dir else SKILLS_USER_DIR
        self.lock = threading.Lock()

    def catalog(self) -> list[SkillMeta]:
        """User entries override bundled entries with the same slug."""
        with self.lock:
            return self._catalog_unlocked()

    def _catalog_unlocked(self) -> list[SkillMeta]:
        by_slug: dict[str, SkillMeta] = {}
        for meta in self._scan(self.bundled_dir, "bundled"):
            by_slug[meta.slug] = meta
        for meta in self._scan(self.user_dir, "user"):
            by_slug[meta.slug] = meta
        return sorted(by_slug.values(), key=lambda m: m.slug)

    def _scan(self, root: Path, source: str) -> list[SkillMeta]:
        if not root.is_dir():
            return []
        found: list[SkillMeta] = []
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            return []
        for entry in entries:
            if not entry.is_dir():
                continue
            slug = entry.name
            if not valid_slug(slug):
                continue
            path = entry / "SKILL.md"
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            fm, body = parse_frontmatter(text)
            name = str(fm.get("name") or slug).strip() or slug
            desc = str(fm.get("description") or "").strip()
            if not desc:
                for para in body.strip().split("\n\n"):
                    line = para.strip().lstrip("#").strip()
                    if line:
                        desc = line.split("\n", 1)[0].strip()
                        break
            tags = _as_tags(fm.get("tags"))
            found.append(
                SkillMeta(
                    slug=slug,
                    name=name,
                    description=desc,
                    tags=tags,
                    source=source,
                    path=path,
                )
            )
        return found

    def search(self, query: str = "", limit: Any = None) -> str:
        q = str(query or "").strip()
        try:
            n = SKILL_SEARCH_DEFAULT_LIMIT if limit is None else int(limit)
        except (TypeError, ValueError):
            n = SKILL_SEARCH_DEFAULT_LIMIT
        n = max(1, min(SKILL_SEARCH_MAX_LIMIT, n))
        catalog = self.catalog()
        if not catalog:
            return (
                "(no skills stored)\n"
                "Write a reusable how-to with skill_write if this procedure "
                "should help future sessions."
            )
        if not q:
            rows = catalog[:SKILL_CATALOG_MAX]
            truncated = len(catalog) > SKILL_CATALOG_MAX
            lines = [self._format_hit(m) for m in rows]
            if truncated:
                lines.append(f"(catalog truncated to {SKILL_CATALOG_MAX} of {len(catalog)})")
            return "\n".join(lines)
        tokens = _tokenize(q)
        ranked = sorted(
            (( _score(m, q, tokens), m) for m in catalog),
            key=lambda pair: (-pair[0], pair[1].slug),
        )
        hits = [m for score, m in ranked if score > 0][:n]
        if not hits:
            slugs = ", ".join(m.slug for m in catalog[:SKILL_CATALOG_MAX])
            return (
                f"No skills matched {q!r}.\n"
                f"Known slugs: {slugs}\n"
                "Broaden the query, or skill_write a new one if this should persist."
            )
        return "\n".join(self._format_hit(m) for m in hits)

    def _format_hit(self, meta: SkillMeta) -> str:
        desc = " ".join((meta.description or "").split())
        if len(desc) > 220:
            desc = desc[:219] + "…"
        tags = f"  tags: {', '.join(meta.tags)}" if meta.tags else ""
        return (
            f"slug: {meta.slug}  source: {meta.source}\n"
            f"  {desc}{tags}"
        )

    def read(self, slug: str) -> str:
        slug = str(slug or "").strip().lower()
        if not valid_slug(slug):
            return f"ERROR: invalid skill slug {slug!r}"
        with self.lock:
            catalog = self._catalog_unlocked()
            match = next((m for m in catalog if m.slug == slug), None)
            if match is None:
                slugs = ", ".join(m.slug for m in catalog[:SKILL_CATALOG_MAX]) or "(none)"
                return f"ERROR: no skill named {slug!r}. Known: {slugs}"
            try:
                text = match.path.read_text(encoding="utf-8")
            except OSError as e:
                return f"ERROR: could not read skill {slug!r}: {e}"
        fm, body = parse_frontmatter(text)
        name = str(fm.get("name") or match.name).strip()
        desc = str(fm.get("description") or match.description).strip()
        header = (
            f"# skill {match.slug} ({match.source})\n"
            f"name: {name}\n"
            f"description: {desc}\n"
        )
        body = body.strip()
        if body:
            return header + "\n" + body + ("\n" if not body.endswith("\n") else "")
        return header + "(empty body)\n"

    def write(
        self,
        slug: str,
        name: str,
        description: str,
        body: str,
        tags: Any = None,
    ) -> str:
        slug = str(slug or "").strip().lower()
        if not valid_slug(slug):
            return (
                "ERROR: skill slug must be 1-63 chars of lowercase letters, "
                "digits, and hyphens, starting with a letter or digit"
            )
        name = str(name or "").strip() or slug
        description = str(description or "").strip()
        if not description:
            return "ERROR: skill_write requires a description (that is the search surface)"
        raw_body = str(body or "")
        if raw_body.lstrip().startswith("---"):
            _fm, raw_body = parse_frontmatter(raw_body)
        raw_body = raw_body.strip()
        if len(raw_body) > SKILL_BODY_MAX_CHARS:
            return (
                f"ERROR: skill body is {len(raw_body)} chars; "
                f"max is {SKILL_BODY_MAX_CHARS}. Keep how-tos compact."
            )
        tag_tuple = _as_tags(tags)
        markdown = render_skill_markdown(
            name=name,
            description=description,
            body=raw_body,
            tags=tag_tuple,
        )
        dest_dir = self.user_dir / slug
        dest = dest_dir / "SKILL.md"
        try:
            if not self.user_dir.exists():
                self.user_dir.mkdir(parents=True, exist_ok=True)
                try:
                    os.chmod(self.user_dir, 0o700)
                except OSError:
                    pass
            dest_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(dest, markdown)
        except OSError as e:
            return f"ERROR: could not write skill {slug!r}: {e}"
        existed_bundled = (self.bundled_dir / slug / "SKILL.md").is_file()
        note = ""
        if existed_bundled:
            note = " (shadows bundled skill of the same slug)"
        return f"wrote skill {slug} -> {dest}{note}"
