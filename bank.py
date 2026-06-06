from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


class ProfileEntry(BaseModel):
    id: str
    text: str
    suitable_for: list[str]


class HighlightEntry(BaseModel):
    id: str
    company: str
    position: str
    status: Literal["active", "inactive"]
    text: str
    tags: list[str]
    suitable_for: list[str]


class ContentBank(BaseModel):
    version: str
    profiles: list[ProfileEntry]
    highlights: list[HighlightEntry]

    @classmethod
    def load(cls, path: Path = Path("content_bank.yaml")) -> "ContentBank":
        return cls.model_validate(yaml.safe_load(path.read_text()))

    def active_highlights(self) -> list[HighlightEntry]:
        return [h for h in self.highlights if h.status == "active"]

    def by_company(self, company: str) -> list[HighlightEntry]:
        return [h for h in self.highlights if h.company == company]

    def tagged(self, *tags: str) -> list[HighlightEntry]:
        tag_set = set(tags)
        return [h for h in self.highlights if tag_set & set(h.tags)]
