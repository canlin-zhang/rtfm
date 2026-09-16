# Running a review

Review is a gate on a finished change. It is not a way to improve a change that
is still moving.

This file exists because of PR #25. A working feature — green CI, tests passing,
the originating issue's query answered — went through four rounds of agent review
and was closed without merging. No defect was ever found in the pre-existing
codebase. Every blocking defect at the end lived in code written *during* the
review, to satisfy the review.

## What went wrong, so the rules make sense

The author and the convener of the review were the same party, reviewing code
written an hour earlier, with no stated merge criterion. Each round's findings
were fixed immediately, in the same branch. Those fixes were new, unreviewed code,
so the next round had fresh material. The loop had no stopping condition, because
adversarial review never runs out of things to say.

The diff grew from roughly 400 lines to 1,855. The feature was the first 400.

## Rules

### 1. Write the merge criterion before the review runs

Not "the reviewers are satisfied." Reviewers are never satisfied; that is their
function. State it as something observable:

> Merges when: CI is green on all supported Pythons, and the query from #24
> returns `cc/private/rules_impl/cc_shared_library.bzl`.

If a finding does not threaten the criterion, it is not a merge blocker. It may
still be a good finding. File it.

### 2. Findings get a recommendation and wait for a call

The reviewer reports. The owner decides. Nothing is fixed proactively, and
nothing is fixed *during* a round.

This is the rule that matters most. Fixing on receipt is what turns review into a
generator: every fix is unreviewed code, and the next round reviews it.

### 3. Cap at three rounds

Same cap as the Copilot review discipline. If round three still produces
blockers, the change is not ready and the answer is to scope it down, not to run
round four.

### 4. Fixes are their own change

A fix for a round-one finding does not go into the branch under review. It is a
separate commit reviewed on its own terms, or a follow-up issue. Once a round's
fixes land in the reviewed diff, the PR is no longer the change anyone agreed to
review.

### 5. The convener should not be the author

If they must be — a solo repo, usually — the cap is **one round**. A single author
reviewing their own fresh work has no independent judgement about when to stop,
and will keep going as long as findings keep arriving.

### 6. Say when it is not converging

If round N's findings are mostly in round N−1's fixes, the review is chasing its
own tail. Say so out loud, stop, and ship what was already good.

## Scoping a review brief

- Name what is settled and not up for review. An accepted ADR is a spec, not a
  proposal. Reviewers asked to re-litigate settled design will do it.
- Name what earlier rounds already fixed, so findings are new ground rather than
  repeats.
- Ask for a merge verdict, not just findings. "Ship" and "hold, because X" are
  different outputs and a reviewer given only "find problems" returns only
  problems.

## Dispatching a review round

Dispatch subagents that invoke the `pr-review-toolkit` skill, and let them fan
out subagents of their own. One reviewer per lens — correctness, tests, silent
failures, comment accuracy, type design — reading the same diff, is how a round
covers ground a single pass misses.

Fan-out is not a substitute for the rules above. More reviewers find more
findings; they do not decide which ones block. A round that ends without a
merge verdict has produced material, not a decision.

## Deciding on a finding

| Finding | Action |
| --- | --- |
| Threatens the merge criterion | Blocks. Fix before merge. |
| Real defect, does not threaten it | File an issue. Merge. |
| Design disagreement with an accepted ADR | Declined, with the ADR cited. |
| Style, naming, structure preference | Weak-reject by default. |

A finding being correct is not the same as a finding being blocking. Most correct
findings are not blocking.
