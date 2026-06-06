from typing import Literal

from rendercv.schema.models.design.classic_theme import ClassicTheme


class SlateTheme(ClassicTheme):
    theme: Literal["slate"] = "slate"
