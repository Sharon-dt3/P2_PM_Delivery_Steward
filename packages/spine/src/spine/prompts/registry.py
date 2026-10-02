"""
Prompt registry (SPN-05).

Every prompt sent to a model anywhere in this codebase lives in its own
versioned file under prompts/<capability>/vN.md, never as a Python string
literal. That is what makes prompt regression detectable: the eval
harness records the exact version string returned here alongside every
result, so a prompt change shows up as a line in the eval history instead
of an invisible diff in model behaviour. tests/unit/test_no_inline_prompts.py
is the lint test that enforces this: no prompt-shaped literal is allowed
to exist outside prompts/.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROMPTS_DIR = Path("prompts")

# A version file is named v<N>.md or v<N>.txt -- nothing else in a
# capability directory is treated as a prompt version.
_VERSION_FILE_RE = re.compile(r"^v(?P<version>\d+)\.(?:md|txt)$")


class PromptNotFoundError(KeyError):
    """Raised when a capability or version has no matching prompt file --
    never silently falls back to a different one."""


@dataclass(frozen=True)
class Prompt:
    """A loaded prompt: the exact template text plus the version string
    the eval harness must record alongside any result produced with it."""

    capability: str
    version: str
    text: str

    def render(self, **values: object) -> str:
        """Fill the template's {placeholder} slots. Raises if the template
        needs a placeholder that wasn't supplied -- never leaves one in
        unfilled, and never silently ignores an unused one either."""
        try:
            return self.text.format(**values)
        except KeyError as exc:
            raise ValueError(
                f"Prompt {self.capability}@{self.version} requires placeholder {exc}, "
                "which was not supplied"
            ) from exc


class PromptRegistry:
    """Loads versioned prompt files from prompts/<capability>/vN.{md,txt}.

    One directory per capability. get() returns the highest version by
    default; pass version= to pin an older one deliberately (e.g. to
    reproduce a past eval run). Nothing here ever falls back to an
    in-code default -- a missing capability or version is an error, not
    a silently-substituted string.
    """

    def __init__(self, prompts_dir: str | Path = DEFAULT_PROMPTS_DIR) -> None:
        self.prompts_dir = Path(prompts_dir)

    def get(self, capability: str, version: str | None = None) -> Prompt:
        capability_dir = self.prompts_dir / capability
        if not capability_dir.is_dir():
            raise PromptNotFoundError(
                f"No prompt directory for capability={capability!r} under {self.prompts_dir}"
            )

        versions = self._list_versions(capability_dir)
        if not versions:
            raise PromptNotFoundError(
                f"No versioned prompt files (vN.md / vN.txt) under {capability_dir}"
            )

        if version is None:
            chosen_version, chosen_path = max(versions, key=lambda item: int(item[0][1:]))
        else:
            matches = [item for item in versions if item[0] == version]
            if not matches:
                available = sorted(v for v, _ in versions)
                raise PromptNotFoundError(
                    f"No version {version!r} for capability={capability!r}; available: {available}"
                )
            chosen_version, chosen_path = matches[0]

        return Prompt(capability=capability, version=chosen_version, text=chosen_path.read_text())

    def list_capabilities(self) -> list[str]:
        if not self.prompts_dir.is_dir():
            return []
        return sorted(p.name for p in self.prompts_dir.iterdir() if p.is_dir())

    def _list_versions(self, capability_dir: Path) -> list[tuple[str, Path]]:
        out = []
        for path in capability_dir.iterdir():
            match = _VERSION_FILE_RE.match(path.name)
            if match:
                out.append((f"v{match.group('version')}", path))
        return out
