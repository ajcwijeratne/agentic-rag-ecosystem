"""Adapter contracts for local and remote media capabilities."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class NotAvailable(RuntimeError):
    """Raised when a requested capability has no configured implementation."""


class Transcriber(ABC):
    @abstractmethod
    def transcribe(self, path: str, **options: Any) -> dict:
        raise NotImplementedError


class ImageGenerator(ABC):
    @abstractmethod
    def generate(self, brief: dict, **options: Any) -> dict:
        raise NotImplementedError


class VideoGenerator(ABC):
    @abstractmethod
    def generate(self, brief: dict, **options: Any) -> dict:
        raise NotImplementedError


class AudioGenerator(ABC):
    @abstractmethod
    def generate(self, brief: dict, **options: Any) -> dict:
        raise NotImplementedError


class DocumentGenerator(ABC):
    @abstractmethod
    def create_from_production(self, production: dict, **options: Any) -> dict:
        raise NotImplementedError

    @abstractmethod
    def copy_from_production(self, production: dict, template_id: str, **options: Any) -> dict:
        raise NotImplementedError


class PresentationGenerator(ABC):
    @abstractmethod
    def create_from_production(self, production: dict, **options: Any) -> dict:
        raise NotImplementedError

    @abstractmethod
    def copy_from_production(self, production: dict, template_id: str, **options: Any) -> dict:
        raise NotImplementedError


class VisualEmbedder(ABC):
    @abstractmethod
    def embed_image(self, path: str) -> list[float]:
        raise NotImplementedError

    @abstractmethod
    def embed_text(self, text: str) -> list[float]:
        raise NotImplementedError
