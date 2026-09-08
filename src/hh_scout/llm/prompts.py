"""Prompt assembly from the `prompts/` directory. Files are re-read on every call by design."""

from __future__ import annotations

from pathlib import Path

_SEP = "\n---\n"


def load_prompt_body(path: Path) -> str:
    """Return the part of a prompt file after the first `---` separator (the header is for humans)."""
    text = path.read_text(encoding="utf-8")
    if _SEP in text:
        return text.split(_SEP, 1)[1].strip()
    return text.strip()


class PrivatePromptMissing(FileNotFoundError):
    """A private (gitignored) prompt file is absent — the owner must create it from the *.example.md."""


def read_private(prompts_dir: Path, name: str) -> str:
    """Read an owner-private file (candidate_profile.md, resume.md) with a helpful error if it is missing."""
    path = prompts_dir / name
    if not path.exists():
        raise PrivatePromptMissing(
            f"Нет файла {path}. Он личный и не хранится в git: скопируйте {name.replace('.md', '.example.md')} "
            f"в {name} и заполните своими данными."
        )
    return path.read_text(encoding="utf-8").strip()


def candidate_profile(prompts_dir: Path) -> str:
    return read_private(prompts_dir, "candidate_profile.md")


def render(prompts_dir: Path, template_name: str, **fields: str) -> str:
    body = load_prompt_body(prompts_dir / template_name)
    fields.setdefault("candidate_profile", candidate_profile(prompts_dir))
    for key, value in fields.items():
        body = body.replace("{" + key + "}", value)
    return body
