from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CardFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    targetWord: str = Field(min_length=1, max_length=2000)
    sentence: str = Field(default="", max_length=20000)
    translation: str = Field(default="", max_length=20000)
    definitions: str = Field(default="", max_length=30000)
    exampleSentences: str = Field(default="", max_length=30000)
    notes: str = Field(default="", max_length=30000)
    images: list[str] = Field(default_factory=list, max_length=10)
    sentenceAudio: list[str] = Field(default_factory=list, max_length=10)
    wordAudio: list[str] = Field(default_factory=list, max_length=10)
    metadata: dict[str, str] = Field(default_factory=dict)


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = Field(default="ja", min_length=2, max_length=20)
    deck_id: int | None = Field(default=None, gt=0, le=2**53 - 1)
    card_type_id: int | None = Field(default=None, gt=0, le=2**53 - 1)
    fields: CardFields


class CardSubmission(Draft):
    deck_id: int = Field(gt=0, le=2**53 - 1)
    card_type_id: int = Field(gt=0, le=2**53 - 1)


class TextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = Field(default="ja", min_length=2, max_length=20)
    text: str = Field(min_length=1, max_length=20000)


class WordFocus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: Literal["ja"] = "ja"
    sentence: str = Field(min_length=1, max_length=20000)
    focus_word: str = Field(min_length=1, max_length=2000)
    # Qt and Migaku use UTF-16 offsets, including for characters outside the BMP.
    focus_start: int = Field(ge=0, le=40000)
    focus_end: int = Field(gt=0, le=40000)

    @model_validator(mode="after")
    def matching_focus(self):
        encoded = self.sentence.encode("utf-16-le")
        try:
            selected = encoded[self.focus_start * 2:self.focus_end * 2].decode("utf-16-le")
        except UnicodeDecodeError:
            raise ValueError("Focus offsets must fall on character boundaries")
        if self.focus_end <= self.focus_start or self.focus_end * 2 > len(encoded) or selected != self.focus_word:
            raise ValueError("Focus offsets must select focus_word within sentence")
        return self


class Ingestion(WordFocus):
    translation: str = Field(default="", max_length=20000)


class DeckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    language: str = Field(default="ja", min_length=2, max_length=20)
    icon: str = Field(default="📚", max_length=20)


WordStatus = Literal["known", "learning", "unknown", "ignored", "tracking"]
