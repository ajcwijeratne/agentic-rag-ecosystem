"""Generation contracts and workers for local multimedia production."""

from .contracts import GenerationJob, GenerationResult
from .dispatcher import generate, generate_dict

__all__ = ["GenerationJob", "GenerationResult", "generate", "generate_dict"]
