"""Minimal DiLoCo + adapter experimentation kit.

P0/P1 implement the algorithm, a toy LoRA backend, an HTTP coordinator, a
worker and crash-safe checkpoints. The Hugging Face backend is the bridge to
real 32B/70B models; it is deliberately optional so the algorithm can be tested
on CPU without downloading anything.
"""

__version__ = "0.1.0"
