---
status: accepted
---

# Shipped skills are corpus-general and carry no vendor/domain tables

The plugin's two skills, both generalized from their original domain-specific form:

- **read-the-manual** (from `check-specs`; *shipped*) — answer strictly from the Corpus (PDF +
  repo + HTML), citing format-native locators (ADR 0004); no model-knowledge fallback. Not PDF-
  or "spec"-specific.
- **audit** (from `audit-specs`; *deferred to Plan 4 with the `rename`/`delete` mutation tools
  it depends on*) — sha256 dedup + title-page version extraction + collision-checked,
  approval-gated rename proposals, operating only on a `mutable` Source (ADR 0001).

Neither skill carries hardcoded vendor or domain taxonomies. The original `audit-specs`
embedded a vendor's product-suite tables (specific product-family prefixes, "product A ≠
product B" rules, vendor revision codes); these are **stripped**. File grouping is purely
heuristic (filename-prefix + detected version string). Generic version-string *patterns*
(semver and common vendor revision shapes) may remain only as illustrative examples of
"what a version looks like," never as a baked-in product catalog.

## Consequences

- The tool stays domain-neutral: a stranger auditing an unrelated PDF pile gets sensible
  heuristic grouping, not dead vendor categories.
- **Guardrail:** contributors must not re-introduce project- or vendor-specific tables into
  shipped skills. Such knowledge belongs in the user's own private config, never the
  published artifact.
- Skills reference the new server's tool names, not `mcp__specs__*`.
