# Research workflow guidance

Keep workflows inspectable and executable section by section. Use concise scripts and explicit source versions and paths. Preserve original observations, identifiers, mapping evidence, unknown values and shared-checklist provenance. Do not infer taxonomic equivalence across versions or treat eBird-derived candidates as verified ABAP cards. Keep audits targeted; do not introduce HTML reporting.

Run `uv sync --frozen`, `uv run python -m unittest discover -s tests -v`, `uv run ruff check bird_atlas scripts tests --select E9,F`, and `uv run python tests/check_notebook.py`. Do not download or publish data automatically.
