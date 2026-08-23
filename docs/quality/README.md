# Quality loop

The atlas is driven to its definition of done by an adversarial review loop
(plan: `docs/superpowers/plans/2026-08-22-clinical-data-atlas-phase0.md`, section M5).
Each **round** runs the workflow `.claude/workflows/quality-round.js` (Claude Code
`Workflow` tool, `name: "quality-round"`, `args: {round, phase, liveUrl, seed}`):

1. **Review** — six independent lens reviewers (data-correctness, pipeline-code,
   site-ux-a11y, performance, security-hygiene, docs) each report at most 8
   evidenced findings with an exact reproduction and a concrete fix.
2. **Verify** — every finding is attacked by skeptics: HIGH/CRITICAL get a
   *refuter* and a *reproducer*, MEDIUM/LOW a reproducer. A finding is
   **confirmed** when at least one skeptic reproduces it and none refutes it;
   a split goes to a tie-breaking judge; unverifiable findings become notes.
3. **Fix** — confirmed findings are grouped by area (disjoint file globs); one
   fixer per area works in its own git worktree, test-first, small conventional
   commits tagged with the finding id. The controller merges the branches
   sequentially (`--no-ff`), re-running the unit suite after each merge, and
   re-materialises data with `make refresh` when a data fix lands (catalog files
   are never hand-edited).
4. **Re-verify** — `make test`, `make site`, `make e2e`, `atlas dod`, and a
   re-run of every confirmed reproduction; unfixed findings carry over with
   `age + 1` (age ≥ 2 is escalated in the report).
5. **Score** — per lens `max(0, 100 − Σ open penalties)` with penalties
   critical 40 / high 15 / medium 5 / low 1; overall = weighted mean
   (data 25, site 20, pipeline 15, performance 10, security 10, docs 10, DoD 10);
   caps: any open critical ⇒ ≤ 59, any open high ⇒ ≤ 84.

**Exit:** two consecutive rounds with zero open high/critical findings **and**
overall score ≥ 90 **and** `make dod PHASE=<n>` green — or six rounds, after
which residuals are reported.

## Files

| file | content |
|---|---|
| `findings.jsonl` | one line per finding: `{id, round, lens, severity, area, file, line, claim, evidence, repro, fix, status, age}`; `status ∈ open, fixed, refuted, note` |
| `scorecard.json` | `{"rounds": [{round, date, commit, scores: {lens: n}, overall, open: {critical, high, medium, low}, dod}]}` |
| `rounds/NN.md` | human-readable round report (what was found, verified, fixed, skipped) |
| `REPORT.md` | final report for the phase (written when the loop exits) |

The loop is paced by the controller session (`/loop`, self-scheduled wake-ups):
red CI/deploy → fix first; open findings → run the next round; clean round →
wait for the deploy to land, then review the live site again.
