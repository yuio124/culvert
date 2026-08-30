# TODO — deliberately out of scope for v0.3.0

Recorded during the v0.3.0 implementation; none of these are required by the
current success criterion ("a deny-induced rewrite/shrink/omission must be
discoverable afterwards from events + transcript").

1. **Worker RESULT provenance** (next-version priority 1): the worker can drop a
   step and still return `status: SUCCESS`. Candidate fields: `commands_run` /
   executed steps / exit-code evidence, enforced by the SubagentStop validator.
2. **Rule words inside string arguments**: patterns fire on prose/data, not just
   executables — observed live twice (a `sed` replacement containing
   `node --test`; a `git commit -m` message mentioning a recursive-search
   trigger). Needs a quoting-aware pass or shape check before pattern match.
3. **Override prefix only works at command start**: `CULVERT_OVERRIDE=...` in the
   middle of a compound command is silently ignored (observed live). Either
   document more loudly or detect-and-hint.
4. **prompt_id resolution**: measured identical across a deny and its same-turn
   retry, but in a single-prompt headless session every call shared one
   prompt_id — it may group by user prompt rather than by assistant turn.
   Verify in an interactive multi-prompt session before leaning on it.
5. **Shadow/replay audit** for counterfactual deny candidates (classifier audit
   only — not a router-readiness test).

6. **Post-deny preservation tracking** (from the first natural-workload case):
   once ~30 denies accumulate, human-judge each as preserved / damaged /
   undecidable, and track override rate per session as a secondary signal.
   No effect claims before that sample exists.
7. **long-inline-python replay/shadow audit**: the observed narrowing incident
   left the rule's own FP question censored (original command never executed).
   Audit the rule's real output distribution before any relaxation.
