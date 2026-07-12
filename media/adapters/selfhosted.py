"""Self-hosted adapter implementations."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from rag import visual_embedder

from .base import (
    AudioGenerator,
    DocumentGenerator,
    ImageGenerator,
    NotAvailable,
    PresentationGenerator,
    Transcriber,
    VideoGenerator,
    VisualEmbedder,
)


class SelfHostedTranscriber(Transcriber):
    def transcribe(self, path: str, **options: Any) -> dict:
        try:
            from media.whisper_pipeline import transcribe_file
        except Exception as exc:
            raise NotAvailable(f"local transcription is unavailable: {exc}") from exc
        return transcribe_file(Path(path), **options)


class SelfHostedImageGenerator(ImageGenerator):
    def generate(self, brief: dict, **options: Any) -> dict:
        raise NotAvailable("self-hosted image generation is not configured")


class SelfHostedVideoGenerator(VideoGenerator):
    def generate(self, brief: dict, **options: Any) -> dict:
        output = brief.get("output_path")
        inputs = brief.get("input_paths") or []
        if not output or not inputs:
            raise NotAvailable("self-hosted video generation needs input_paths and output_path")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(inputs[0]), "-c", "copy", str(output)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise NotAvailable(result.stderr[-1000:])
        return {"path": str(output)}


class SelfHostedAudioGenerator(AudioGenerator):
    def generate(self, brief: dict, **options: Any) -> dict:
        raise NotAvailable("self-hosted audio generation is not available")


class SelfHostedDocumentGenerator(DocumentGenerator):
    def create_from_production(self, production: dict, **options: Any) -> dict:
        raise NotAvailable("self-hosted document design is not configured")

    def copy_from_production(self, production: dict, template_id: str, **options: Any) -> dict:
        raise NotAvailable("self-hosted document design is not configured")


class SelfHostedPresentationGenerator(PresentationGenerator):
    def create_from_production(self, production: dict, **options: Any) -> dict:
        raise NotAvailable("self-hosted presentation design is not configured")

    def copy_from_production(self, production: dict, template_id: str, **options: Any) -> dict:
        raise NotAvailable("self-hosted presentation design is not configured")


class SelfHostedVisualEmbedder(VisualEmbedder):
    def embed_image(self, path: str) -> list[float]:
        return visual_embedder.embed_image(path)

    def embed_text(self, text: str) -> list[float]:
        return visual_embedder.embed_text(text)
