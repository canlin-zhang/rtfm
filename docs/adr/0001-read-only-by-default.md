---
status: accepted
---

# Read-only by default; mutation gated by a per-source `mutable` flag

The server indexes documents **in place**, including licensed vendor installs it must
neither modify nor redistribute, so indexing and search are strictly read-only. The
file-mutating tools (`rename`, `delete`) and the audit/reorg skills are kept because
they're valuable, but they never run autonomously and never touch a Source unless that
Source is explicitly marked `mutable: true` in the Manifest — a flag only the user may set,
by editing the Manifest. The bootstrap Default source ships `mutable: true`; every other
Source defaults to `false`.

## Considered Options

- **A — Pure consent gate.** Mutation allowed on any Source whenever the user explicitly
  asks. Rejected: an audit approval can authorize a *blind* rename across a Source the user
  has forgotten points at a vendor install. The hazard is mutating-without-seeing, which a
  transient "yes" doesn't prevent.
- **B — Capability floor via copy.** Mutation only on owned dirs; to curate vendor PDFs you
  must first copy them into a managed Source. Rejected: the forced copy workflow is
  paternalistic toward a licensed user who legitimately wants to rename files in place.
- **C — Durable per-source opt-in (chosen).** Mutation allowed on any Source, but only after
  the user records intent by setting `mutable: true` in the Manifest. No copy step; the
  recorded flag is the explicit, auditable form of "the user asked."

## Consequences

- Search and indexing behave identically for every Source; the flag touches only the
  rename/delete/audit path, so no read-path mode complexity is introduced.
- The complete set of directories the tool may ever modify is readable from the Manifest.
- The legality posture is structural: out of the box the tool cannot modify a vendor tree;
  only the licensed user, by editing their own config, can authorize it.
- Hard rule for the server prompt and skills: the agent must NEVER set `mutable` itself.
