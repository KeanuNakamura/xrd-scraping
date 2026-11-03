"""Version pins for enrichment resume / cache invalidation."""

from __future__ import annotations

# Bump when structured analysis schema fields change in a breaking way.
ANALYSIS_SCHEMA_VERSION = "v7"

# Bump when SYSTEM_PROMPT / user prompt assembly for figure analysis changes.
ANALYSIS_PROMPT_VERSION = "v7"

# Disk cache filename used by OpenAIVisionClient for per-figure enrichment.
ANALYSIS_CACHE_NAME = f"xrd_analysis_{ANALYSIS_SCHEMA_VERSION}.json"

# Bump when paper-level synthesis prompt/schema changes.
PAPER_ANALYSIS_CACHE_NAME = "paper_analysis_v3.json"

# Shared with legacy/figure_triage.py classify cache keys.
XRD_CLASSIFY_PROMPT_VERSION = "v3_caption_hints"
TRIAGE_PROMPT_VERSION = "v1"
COMBINED_CLASSIFY_TRIAGE_PROMPT_VERSION = "v1_combined"
