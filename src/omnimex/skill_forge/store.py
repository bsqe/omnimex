"""Storage archive, ZIP export, and multi-agent installation for packaged SKILL.md skills."""

from __future__ import annotations

import contextlib
import io
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimex.core.models import (
    SavedSkillSummary,
    SkillInstallEntry,
    SkillInstallResult,
    SkillInstallTarget,
)
from omnimex.core.redaction import redact_text
from omnimex.setup.installer import write_file_with_backup
from omnimex.skill_forge.builder import (
    format_skill_frontmatter,
    parse_skill_frontmatter,
    slugify_skill_name,
)

SKILL_TARGET_RELATIVE_DIRS: dict[SkillInstallTarget, tuple[str, ...]] = {
    "antigravity": (".gemini", "antigravity", "skills"),
    "claude_code": (".claude", "skills"),
    "codex": (".codex", "skills"),
    "cursor": (".cursor", "skills"),
    "gemini_cli": (".gemini", "skills"),
    "windsurf": (".codeium", "windsurf", "skills"),
}


def resolve_skill_target_path(
    target: SkillInstallTarget,
    slug: str,
    *,
    home_dir: Path | None = None,
) -> Path:
    """Resolve ``<agent_skills_dir>/<slug>/SKILL.md`` for a supported target agent."""
    rel = SKILL_TARGET_RELATIVE_DIRS.get(target)
    if rel is None:
        raise ValueError(f"Unsupported skill target: {target}")
    clean_slug = slugify_skill_name(slug)
    home = (home_dir or Path.home()).expanduser().resolve()
    return home.joinpath(*rel, clean_slug, "SKILL.md")


def _normalize_skill_markdown(slug: str, skill_md: str) -> tuple[str, str, str]:
    clean_slug = slugify_skill_name(slug)
    redacted = redact_text(skill_md.strip())
    parsed_name, parsed_desc, body = parse_skill_frontmatter(redacted)
    if parsed_name is None:
        final_md = format_skill_frontmatter(clean_slug, parsed_desc) + "\n" + body + "\n"
        return clean_slug, parsed_desc, final_md

    if parsed_name != clean_slug:
        final_md = format_skill_frontmatter(clean_slug, parsed_desc) + "\n" + body + "\n"
    else:
        final_md = redacted + "\n"
    return clean_slug, parsed_desc, final_md


def save_skill(skills_dir: Path, slug: str, skill_md: str) -> SavedSkillSummary:
    """Persist a curated skill into ``<skills_dir>/<slug>/SKILL.md`` safely."""
    clean_slug, parsed_desc, final_md = _normalize_skill_markdown(slug, skill_md)
    base_dir = skills_dir.resolve()
    target_path = (base_dir / clean_slug / "SKILL.md").resolve()
    if base_dir not in target_path.parents:
        raise ValueError(f"Unsafe skill slug path: {slug!r}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(final_md, encoding="utf-8")
    stat = target_path.stat()
    return SavedSkillSummary(
        slug=clean_slug,
        name=clean_slug,
        description=parsed_desc,
        file_path=str(target_path).replace("\\", "/"),
        updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        token_estimate=max(1, len(final_md) // 4),
        skill_md=final_md,
    )


def list_saved_skills(skills_dir: Path) -> list[SavedSkillSummary]:
    """List saved skills in ``<skills_dir>/*/SKILL.md`` newest-first."""
    if not skills_dir.is_dir():
        return []

    items: list[SavedSkillSummary] = []
    for child in skills_dir.iterdir():
        if not child.is_dir():
            continue
        skill_file = child / "SKILL.md"
        if not skill_file.is_file():
            continue
        content = skill_file.read_text(encoding="utf-8-sig")
        parsed_name, parsed_desc, _ = parse_skill_frontmatter(content)
        stat = skill_file.stat()
        items.append(
            SavedSkillSummary(
                slug=child.name,
                name=parsed_name or child.name,
                description=parsed_desc,
                file_path=str(skill_file.resolve()).replace("\\", "/"),
                updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                token_estimate=max(1, len(content) // 4),
                skill_md=content,
            )
        )
    items.sort(key=lambda s: s.updated_at, reverse=True)
    return items


def delete_saved_skill(skills_dir: Path, slug: str) -> bool:
    """Delete a saved skill folder from ``<skills_dir>/<slug>``."""
    clean_slug = slugify_skill_name(slug)
    base_dir = skills_dir.resolve()
    skill_dir = (base_dir / clean_slug).resolve()
    if skill_dir.parent != base_dir or not skill_dir.is_dir():
        return False

    skill_file = skill_dir / "SKILL.md"
    if skill_file.is_file():
        skill_file.unlink()
    with contextlib.suppress(OSError):
        skill_dir.rmdir()
    return True


def build_skill_zip_bytes(slug: str, skill_md: str) -> tuple[str, bytes]:
    """Package ``<slug>/SKILL.md`` into an in-memory standard ZIP archive."""
    clean_slug, _, final_md = _normalize_skill_markdown(slug, skill_md)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{clean_slug}/SKILL.md", final_md.encode("utf-8"))
    return clean_slug, buf.getvalue()


def install_packaged_skill(
    slug: str,
    skill_md: str,
    *,
    targets: Sequence[SkillInstallTarget] = (),
    custom_dir: Path | None = None,
    home_dir: Path | None = None,
) -> SkillInstallResult:
    """Install ``<slug>/SKILL.md`` into selected coding-agent skill folders with ``.bak`` backups."""
    clean_slug, _, final_md = _normalize_skill_markdown(slug, skill_md)
    resolved_targets: list[tuple[str, Path]] = []

    for target in dict.fromkeys(targets):
        resolved_targets.append(
            (target, resolve_skill_target_path(target, clean_slug, home_dir=home_dir))
        )

    if custom_dir is not None:
        custom_root = Path(custom_dir).expanduser().resolve()
        resolved_targets.append(("custom", custom_root / clean_slug / "SKILL.md"))

    if not resolved_targets:
        raise ValueError("Select at least one target agent or custom directory to install the Skill.")

    entries: list[SkillInstallEntry] = []
    errors: list[str] = []

    for label, dest in resolved_targets:
        try:
            existed = dest.is_file()
            if existed:
                existing_content = dest.read_text(encoding="utf-8-sig")
                if existing_content == final_md:
                    entries.append(
                        SkillInstallEntry(
                            target=label,
                            path=str(dest).replace("\\", "/"),
                            status="unchanged",
                            backup_path=None,
                        )
                    )
                    continue
            backup = write_file_with_backup(dest, final_md)
            entries.append(
                SkillInstallEntry(
                    target=label,
                    path=str(dest).replace("\\", "/"),
                    status="updated" if existed else "created",
                    backup_path=str(backup).replace("\\", "/") if backup else None,
                )
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label} ({dest}): {exc}")

    return SkillInstallResult(slug=clean_slug, installed=entries, errors=errors)


__all__ = [
    "SKILL_TARGET_RELATIVE_DIRS",
    "build_skill_zip_bytes",
    "delete_saved_skill",
    "install_packaged_skill",
    "list_saved_skills",
    "resolve_skill_target_path",
    "save_skill",
]
