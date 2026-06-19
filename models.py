from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any


class SelectiveLogic(IntEnum):
    AND_ANY = 0
    NOT_ALL = 1
    NOT_ANY = 2
    AND_ALL = 3


class Position(IntEnum):
    BEFORE_CHARACTER = 0
    AFTER_CHARACTER = 1
    AUTHOR_NOTE_TOP = 2
    AUTHOR_NOTE_BOTTOM = 3
    AT_DEPTH = 4
    EXAMPLE_TOP = 5
    EXAMPLE_BOTTOM = 6
    OUTLET = 7


@dataclass(slots=True)
class LoreEntry:
    uid: str
    content: str
    comment: str = ""
    keys: list[str] = field(default_factory=list)
    secondary_keys: list[str] = field(default_factory=list)
    constant: bool = False
    disabled: bool = False
    selective: bool = False
    selective_logic: int = int(SelectiveLogic.AND_ANY)
    position: int = int(Position.AFTER_CHARACTER)
    depth: int = 4
    role: str = "system"
    order: int = 100
    probability: int = 100
    use_probability: bool = True
    scan_depth: int | None = None
    case_sensitive: bool = False
    match_whole_words: bool = False
    use_regex: bool = False
    sticky: int = 0
    cooldown: int = 0
    delay: int = 0
    exclude_recursion: bool = False
    prevent_recursion: bool = False
    delay_until_recursion: bool = False
    outlet_name: str = ""
    vectorized: bool = False
    group: str = ""
    group_override: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ActivatedEntry:
    entry: LoreEntry
    reason: str
    score: float = 0.0
    recursion_step: int = 0


@dataclass(slots=True)
class ScanResult:
    activated: list[ActivatedEntry] = field(default_factory=list)
    outlets: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def entries(self) -> list[LoreEntry]:
        return [item.entry for item in self.activated]


@dataclass(slots=True)
class PromptBlock:
    identifier: str
    name: str
    content: str
    role: str = "system"
    enabled: bool = True
    position: str = "system"
    depth: int = 0
    priority: int = 50
    marker: bool = False
    source: str = "preset"
    token_estimate: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BuildResult:
    system_prompt: str
    contexts: list[dict[str, Any]]
    blocks: list[PromptBlock]
    dropped: list[str]
    warnings: list[str]
    messages: list[dict[str, Any]]

