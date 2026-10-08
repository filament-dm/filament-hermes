"""Compatibility import path for installations predating the filament rename."""

from pathlib import Path

# Resolve old submodule imports from the maintained package, including when this
# directory plugin is loaded under Hermes's private package namespace.
__path__ = [str(Path(__file__).resolve().parent.parent / "filament")]


def register(ctx):
    from filament import register as canonical_register  # noqa: PLC0415

    return canonical_register(ctx)
