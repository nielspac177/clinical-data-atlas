export const meta = {
  name: 'quality-round',
  description: 'Adversarial quality round: lens reviewers -> skeptic verification -> area fixers in worktrees',
  whenToUse: 'Run one round of the Clinical Data Atlas quality loop (see docs/quality/README.md). args = {round, liveUrl, repo, seed, phase, prevFindingsPath, maxFindingsPerLens, skipFix}',
  phases: [
    { title: 'Review', detail: 'six lens reviewers produce findings' },
    { title: 'Verify', detail: 'skeptics try to reproduce / refute each finding' },
    { title: 'Fix', detail: 'one fixer per area, each in its own worktree' },
  ],
}

const A = args || {}
const ROUND = A.round || 1
const REPO = A.repo || '/Users/nielspacheco/clinical-data-atlas'
const LIVE = A.liveUrl || 'https://nielspac177.github.io/clinical-data-atlas/'
const SEED = A.seed ?? ROUND
const PHASE = A.phase ?? 0
const MAX_PER_LENS = A.maxFindingsPerLens || 8
const PREV = A.prevFindingsPath || `${REPO}/docs/quality/findings.jsonl`
const SKIP_FIX = !!A.skipFix

const SEVERITIES = ['critical', 'high', 'medium', 'low']
const AREAS = ['harvest_normalize', 'enrich_graph_refresh', 'site', 'docs', 'ci', 'tests', 'data']
const AREA_GLOBS = {
  harvest_normalize: 'atlas/harvest/**, atlas/normalize/**, atlas/http.py, atlas/io.py, tests/test_harvest_*.py, tests/test_normalize_*.py, tests/fixtures/<source>/**, docs/sources*.md',
  enrich_graph_refresh: 'atlas/enrich/**, atlas/graph/**, atlas/refresh.py, atlas/diff.py, atlas/cli.py, atlas/schema.py, atlas/vocab.py, atlas/config.py, tests/test_{rules,llm,mesh_ror,dedupe,graph,diff,refresh,cli,schema}*.py',
  site: 'site/**, atlas/sitebuild.py, tests/js/**, tests/test_sitebuild.py',
  docs: 'README.md, CLAUDE.md, docs/** (except docs/quality/**), CITATION.cff',
  ci: '.github/**, Makefile, pyproject.toml',
  tests: 'tests/e2e/**, tests/conftest.py',
  data: 'code fix in the responsible module (never hand-edit data/**) + note that data must be re-materialised by the controller',
}

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string', description: 'lens-prefixed slug, e.g. site-03' },
          lens: { type: 'string' },
          severity: { type: 'string', enum: SEVERITIES },
          area: { type: 'string', enum: AREAS },
          file: { type: 'string', description: 'repo-relative path, or URL/record id for data/site findings' },
          line: { type: ['integer', 'null'] },
          claim: { type: 'string', description: 'one-sentence defect statement' },
          evidence: { type: 'string', description: 'what you observed, with exact values/output' },
          repro: { type: 'string', description: 'exact command(s) or steps that reproduce it from a clean checkout' },
          fix: { type: 'string', description: 'concrete suggested fix' },
        },
        required: ['id', 'lens', 'severity', 'area', 'file', 'claim', 'evidence', 'repro', 'fix'],
      },
    },
    notes: { type: 'string', description: 'what you checked that was fine, and anything you could not check' },
  },
  required: ['findings', 'notes'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['reproduced', 'refuted', 'unverifiable'] },
    notes: { type: 'string' },
    suggested_severity: { type: ['string', 'null'], enum: [...SEVERITIES, null] },
  },
  required: ['verdict', 'notes'],
}

const FIX_SCHEMA = {
  type: 'object',
  properties: {
    branch: { type: 'string' },
    worktree: { type: 'string' },
    commits: { type: 'array', items: { type: 'string' } },
    fixed_ids: { type: 'array', items: { type: 'string' } },
    skipped: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, reason: { type: 'string' } }, required: ['id', 'reason'] } },
    tests: { type: 'string', description: 'commands run and results' },
    notes: { type: 'string' },
  },
  required: ['branch', 'commits', 'fixed_ids', 'skipped', 'tests'],
}

const COMMON = `Repository: ${REPO} (main checkout; do NOT modify files there — read-only for reviewers). Live site: ${LIVE}. Phase ${PHASE}, quality round ${ROUND}.
Ground rules: you are an adversarial reviewer; your job is to find REAL defects with evidence and an exact reproduction, not to praise. Prefer fewer, well-evidenced findings over many vague ones (max ${MAX_PER_LENS}; rank by severity). Severity: critical = wrong/fabricated data shown to users, data loss, security hole, site unusable; high = a core flow broken or a principle of PROJECT_BRIEF.md violated; medium = clear defect with workaround; low = polish. Every finding needs: exact file:line (or record id / URL), what you observed (paste the actual values), an exact repro, and a concrete fix. Read PROJECT_BRIEF.md, CLAUDE.md and docs/schema.md first for the intended behaviour. Previously reported findings with their status are in ${PREV} (JSONL; may be empty) — re-check items with status "fixed" from earlier rounds that belong to your lens and report regressions as new findings referencing the old id; do not re-report open items verbatim (reference them instead). Do not use data from outside the repo/live site as ground truth except the original sources' own APIs/pages. Tests: unit tests run with ATLAS_OFFLINE=1 (see tests/conftest.py); \`uv run pytest -q -m "not live and not e2e"\`. Playwright is installed (chromium args --use-angle=swiftshader --enable-unsafe-swiftshader). Never hand-edit anything under data/.`

const LENSES = [
  {
    key: 'data',
    prompt: `${COMMON}
LENS: data-correctness. Seed: ${SEED}. Pick 20 records from data/catalog/catalog.jsonl with python (random.seed(${SEED}); random.sample over the list sorted by id) plus every record id mentioned in previously fixed data findings. For each: open the record's \`url\` (curl -sL, follow redirects) and, where a public API exists, the source API (OpenNeuro GraphQL, PhysioNet /api/v1/project/published/, GDC /projects/<id>?expand=..., TCIA NBIA/DataCite) and compare name, access tier, license, sample_size/unit, modalities, conditions, years, summary faithfulness (no claims absent from the source), and provenance fields. Hunt for fabrication: any field value not derivable from the raw envelope in data/raw/<source>/records/<id>.json or the LLM cache in data/raw/enrich/llm/ (cache key = provenance.enrichment? check atlas/enrich/llm.py for how to find it). Also check: dedupe merges (data/catalog/excluded.jsonl merged_into entries — are they true duplicates?), same_cohort related links, MeSH ids (resolve 10 of them at https://id.nlm.nih.gov/mesh/<id>.json), access semantics per docs/schema.md, and rules false positives (e.g. the condition term "depression" matching "ST depression" in ECG datasets — search the catalog for suspicious domain/condition assignments and quantify). Report systematic problems as ONE finding with counts and example ids, not 20 separate findings.`,
  },
  {
    key: 'pipeline',
    prompt: `${COMMON}
LENS: pipeline-code. Review atlas/ for bugs: silent excepts, nondeterminism (dict ordering, sets, timestamps in outputs), idempotency (run \`uv run atlas refresh --offline --skip-enrich --dry-run\` or the equivalent and check the diff would be empty — read atlas/refresh.py for the flags; do not write to data/ — if a command would write, copy the repo to /tmp first: \`git -C ${REPO} worktree\` is NOT allowed; use \`rsync -a --exclude .venv --exclude _site ${REPO}/ /tmp/cda_pipe_review/\` then \`uv sync\` there), per-source isolation (one source failing must not kill the run), retries/timeouts/politeness in atlas/http.py, shrink guard in RawStore, cache-key stability for the LLM cache, the guards in atlas/enrich/classify.py (are legitimate LLM outputs dropped — e.g. summary digits rule — quantify from data/raw/enrich/llm vs catalog), dedupe false merges/misses, exit codes, and the CLI surface. Run the unit suite and ruff. Look for code paths untested by tests that matter.`,
  },
  {
    key: 'site',
    prompt: `${COMMON}
LENS: site-ux-a11y. Use Playwright against the LIVE site ${LIVE} (and, if it differs, a local build: \`make site\` writes _site/ — do not run make in the main checkout; rsync the repo to /tmp/cda_site_review first or just use the live site). Check all 5 pages at 1280x800 and 375x812 in dark and light: console errors (fail on any), network 404s, horizontal page scroll (document.documentElement.scrollWidth <= innerWidth), keyboard path (Tab order, visible focus ring, skip link, Esc closes panel, focus return), ARIA (combobox/listbox, aria-sort, aria-pressed theme toggle, live region announcements), colour contrast (compute contrast of text vs background for badges/chips/muted text — WCAG AA 4.5:1), label overlap/density on the graph (take screenshots and LOOK at them), panel content correctness (open 5 datasets: fields, links, access badge), table behaviour with real data (filters, sort, CSV, long names, 2,655 rows performance), What's new rendering, About page links (all 2xx), 404 page, OG/meta tags, base-path correctness of every asset URL, reduced-motion. Save screenshots under /tmp/cda_site_review_r${ROUND}/ and cite them.`,
  },
  {
    key: 'performance',
    prompt: `${COMMON}
LENS: performance. Measure on the live site with Playwright (CDP performance metrics / resource timing): bytes transferred per page (HTML, CSS, JS, vendor, graph.json, search-index.json, records), time to first backbone render, frame time during 10 s of graph simulation (requestAnimationFrame deltas), memory after expanding 3 hubs, table render time for the full catalog, search latency. Compare against budgets: graph.json <= 2.5 MB raw, search-index <= 2 MB, first backbone < 3 s on a simulated Fast 3G-ish profile (use CDP Network.emulateNetworkConditions), no long tasks > 200 ms after load. Inspect atlas/graph/build.py and site/assets/js/{graph,graph-index,labels,table}.js for algorithmic waste (O(n^2) loops over nodes, per-frame DOM churn, unthrottled resize). Report numbers.`,
  },
  {
    key: 'security',
    prompt: `${COMMON}
LENS: security-hygiene. Check: secrets or tokens anywhere (git log -p grep for api keys; .github workflows; config); robots.txt / ToS compliance per source (verify the four sources' robots.txt allow the endpoints used; UA string set; per-host delay honoured — read atlas/http.py and harvesters); no dataset DATA downloaded/rehosted (grep for downloads, file sizes in data/raw, record payloads must be metadata only); pinned dependencies and actions (SHA pins with comments; uv.lock committed; playwright/browser install pinned?); GitHub Actions permissions minimal (contents/pages/id-token/pull-requests per job), no pull_request_target misuse, bot PR workflow cannot be hijacked by untrusted input; HTML injection paths in the site (record fields rendered with innerHTML? changelog markdown sanitizer allowlist in atlas/sitebuild.py — try a malicious fixture changelog in a /tmp copy); CSP/meta headers feasible on Pages; vendored JS integrity (SHA256SUMS matches file, version stated); CITATION/licensing consistency (code MIT, data CC BY 4.0; third-party notices). Provide exact evidence.`,
  },
  {
    key: 'docs',
    prompt: `${COMMON}
LENS: docs. Verify docs/sources.md against the code (each endpoint string in atlas/harvest/*.py appears in the doc; verified dates; deviations from PROJECT_BRIEF.md recorded), docs/schema.md drift (\`uv run atlas schema --check\`), README quickstart from scratch in a temp clone (\`git clone https://github.com/nielspac177/clinical-data-atlas /tmp/cda_docs_review_r${ROUND} && cd there && uv sync --locked --group dev && uv run pytest -q -m "not live and not e2e" && make site\`; every command in README must work as written; remove the clone afterwards), CLAUDE.md accuracy (paths, commands), About page claims vs reality (counts, methodology, cite text, issue-form links), CITATION.cff validity (cffconvert), CHANGELOG/whats-new readability for a human, issue templates usable (fields match schema), LICENSE files present. Findings must quote the wrong text and the correct text.`,
  },
]

phase('Review')
log(`Round ${ROUND}: dispatching ${LENSES.length} lens reviewers`)
const reviews = await parallel(LENSES.map(l => () =>
  agent(l.prompt, { label: `review:${l.key}`, phase: 'Review', schema: FINDINGS_SCHEMA, model: 'opus' })
    .then(r => ({ key: l.key, ...r }))))
const lensResults = reviews.filter(Boolean)
let findings = lensResults.flatMap(r => r.findings.slice(0, MAX_PER_LENS).map(f => ({ ...f, lens: r.key, round: ROUND })))
// normalise ids to be unique and lens-prefixed
const seen = new Set()
findings = findings.map((f, i) => {
  let id = `r${ROUND}-${f.lens}-${String(i + 1).padStart(2, '0')}`
  while (seen.has(id)) id += 'x'
  seen.add(id)
  return { ...f, orig_id: f.id, id }
})
log(`Review done: ${findings.length} findings (${SEVERITIES.map(s => `${s}:${findings.filter(f => f.severity === s).length}`).join(' ')})`)

phase('Verify')
function skepticPrompt(f, mode) {
  const goal = mode === 'refute'
    ? 'Your job is to REFUTE this finding: show it is not a real defect, is already handled elsewhere, contradicts the spec, or the evidence is wrong. Default to "refuted" ONLY with concrete proof; if you cannot refute and can reproduce, say "reproduced"; if you cannot decide, "unverifiable".'
    : 'Your job is to REPRODUCE this finding exactly as described (run the repro). Say "reproduced" only if you actually observed the defect; "refuted" if the repro shows correct behaviour; "unverifiable" if the repro cannot be run.'
  return `${COMMON}
SKEPTIC (${mode}). ${goal}
Finding ${f.id} [${f.severity}] lens=${f.lens} area=${f.area}
file: ${f.file}${f.line ? ':' + f.line : ''}
claim: ${f.claim}
evidence: ${f.evidence}
repro: ${f.repro}
suggested fix: ${f.fix}
Run the repro for real (read-only in ${REPO}; if a command would write, rsync the repo to /tmp/cda_skeptic_${f.id}/ first, excluding .venv/_site, and run there). Also judge whether the severity is right (suggest a different one if so). Be concrete: paste the output you saw.`
}
const verified = await parallel(findings.map(f => () => {
  const modes = (f.severity === 'critical' || f.severity === 'high') ? ['refute', 'reproduce'] : ['reproduce']
  return parallel(modes.map(m => () => agent(skepticPrompt(f, m), { label: `verify:${f.id}:${m}`, phase: 'Verify', schema: VERDICT_SCHEMA, model: 'sonnet' })))
    .then(async vs => {
      const votes = vs.filter(Boolean)
      const rep = votes.filter(v => v.verdict === 'reproduced').length
      const ref = votes.filter(v => v.verdict === 'refuted').length
      let status
      if (rep >= 1 && ref === 0) status = 'confirmed'
      else if (rep === 0 && ref >= 1) status = 'refuted'
      else if (rep >= 1 && ref >= 1) {
        const judge = await agent(skepticPrompt(f, 'reproduce') + `\nTwo earlier skeptics disagreed (${votes.map(v => v.verdict + ': ' + v.notes.slice(0, 300)).join(' | ')}). You are the tie-breaker: run it yourself and decide.`, { label: `judge:${f.id}`, phase: 'Verify', schema: VERDICT_SCHEMA, model: 'opus' })
        status = judge && judge.verdict === 'reproduced' ? 'confirmed' : (judge && judge.verdict === 'refuted' ? 'refuted' : 'note')
        votes.push(judge || { verdict: 'unverifiable', notes: 'judge failed' })
      } else status = 'note'
      const sugg = votes.map(v => v.suggested_severity).filter(Boolean)
      const severity = sugg.length && sugg.every(s => s === sugg[0]) ? sugg[0] : f.severity
      return { ...f, severity, status, votes }
    })
}))
const all = verified.filter(Boolean)
const confirmed = all.filter(f => f.status === 'confirmed')
log(`Verify done: confirmed ${confirmed.length}, refuted ${all.filter(f => f.status === 'refuted').length}, note ${all.filter(f => f.status === 'note').length}`)

phase('Fix')
let fixes = []
if (!SKIP_FIX && confirmed.length) {
  const byArea = {}
  for (const f of confirmed) (byArea[f.area] ||= []).push(f)
  const areas = Object.keys(byArea)
  log(`Fix: ${areas.length} area fixers (${areas.map(a => `${a}:${byArea[a].length}`).join(', ')})`)
  fixes = await parallel(areas.map(area => () => agent(`You are the FIXER for area "${area}" in the Clinical Data Atlas quality loop, round ${ROUND}. You are in your own git worktree of ${REPO} on your own branch (check \`git branch --show-current\`; report it). Fix ONLY the confirmed findings below, touching only files matching this area's globs: ${AREA_GLOBS[area]}. If a finding truly requires a file outside your globs, skip it and say why (another fixer owns it). Never hand-edit data/** — for data findings fix the responsible code and add a test; the controller re-materialises data. Process per finding: write a failing test (or assertion/repro) first, see it fail, fix, see it pass; keep commits small and conventional: "fix(${area}): <what> [${'<finding id>'}]". Run \`uv run pytest -q -m "not live and not e2e"\`, \`uv run ruff check . && uv run ruff format --check .\`, \`node --test tests/js/*.test.mjs\` (if JS touched), and \`make site\` (if site/sitebuild touched) before finishing. Do not merge or push. Findings (JSON):\n${JSON.stringify(byArea[area].map(f => ({ id: f.id, severity: f.severity, file: f.file, line: f.line, claim: f.claim, evidence: f.evidence, repro: f.repro, fix: f.fix, skeptic_notes: f.votes.map(v => v.notes).join(' || ').slice(0, 1500) })), null, 1)}\nReport: branch, worktree path (pwd), commits (hashes + subjects), fixed_ids, skipped (id + reason), tests (commands + results), notes.`, { label: `fix:${area}`, phase: 'Fix', schema: FIX_SCHEMA, isolation: 'worktree', model: 'opus' }).then(r => r && { area, ...r })))
  fixes = fixes.filter(Boolean)
  log(`Fix done: ${fixes.length} fixer branches`)
}

const summary = {
  round: ROUND,
  phase: PHASE,
  counts: { findings: all.length, confirmed: confirmed.length, refuted: all.filter(f => f.status === 'refuted').length, note: all.filter(f => f.status === 'note').length },
  lens_notes: Object.fromEntries(lensResults.map(r => [r.key, r.notes])),
  findings: all.map(f => ({ id: f.id, round: f.round, lens: f.lens, severity: f.severity, area: f.area, file: f.file, line: f.line ?? null, claim: f.claim, evidence: f.evidence, repro: f.repro, fix: f.fix, status: f.status, votes: f.votes.map(v => ({ verdict: v.verdict, notes: v.notes.slice(0, 600) })) })),
  fixes,
}
return summary
