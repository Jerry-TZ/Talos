# Talos recall frozen benchmark

This kit compares the current spreading-activation scorer with an exact no-spread
ablation. It refuses synthetic queries: every case must resolve to one user message in
`.talos/sessions/*.jsonl`.

Check the exact-test arithmetic first:

```powershell
python recall_benchmark.py selftest
```

## 1. Extract candidates

Use the last commit that changed/tuned recall scoring as the cutoff. A whole session is
excluded when its timestamped filename is at or before the cutoff; messages have no own
timestamp, so treating a pre-cutoff session as held out would be unverifiable.

```powershell
python recall_benchmark.py extract `
  --repo D:\projects\ClaudeCode_projects\github_find\talos-public `
  --cutoff-commit 9296ab5 `
  --out candidates.jsonl
```

The command deliberately exits `2` when fewer than 32 provably held-out unique queries
exist. It still writes the candidate audit file. A repeated post-cutoff query is ineligible
if the same hash existed before the cutoff.

## 2. Label without rewriting queries

```powershell
python recall_benchmark.py template --candidates candidates.jsonl --out dataset.draft.jsonl
```

Each JSONL case has this fixed shape:

```json
{
  "id": "T01",
  "kind": "task",
  "query": "exact session text",
  "query_sha256": "full sha256",
  "source": {"session": "...jsonl", "user_ordinal": 1},
  "expected": ["correct-skill.md"],
  "judgments": [
    {"reviewer": "alice", "expected": ["correct-skill.md"], "reason": "concrete reason"},
    {"reviewer": "bob", "expected": ["correct-skill.md"], "reason": "concrete reason"}
  ]
}
```

`expected` is either one skill filename or `[]` for abstain. Two independent reviewers
must agree. The final dataset contains exactly 16 `task` cases and 16 stress cases, where a
stress case is either:

- `paraphrase`: a real, separately observed user query with `pair_id` pointing to a task;
- `hard_negative`: a real query that should abstain but resembles an existing
  `confuser_skill`.

A hard negative must satisfy both human semantics and a mechanical surface check:

- `expected` is `[]`;
- two reviewers explain why no existing skill materially helps;
- at least two keywords overlap the confuser's `description + ## 何时用`;
- overlap containment is at least 0.08.

These lexical rules do not decide correctness. They only stop an obviously unrelated
negative from being called “hard.”

## 3. Freeze

```powershell
python recall_benchmark.py freeze `
  --repo D:\projects\ClaudeCode_projects\github_find\talos-public `
  --dataset dataset.jsonl `
  --cutoff-commit 9296ab5 `
  --out manifest.json
```

Freeze verifies all query texts against the cited session messages and records hashes for
the dataset, `recall.py`, memory, skills, and sessions. Evaluation refuses to run if any of
them changes.

## 4. Evaluate

```powershell
python recall_benchmark.py run `
  --repo D:\projects\ClaudeCode_projects\github_find\talos-public `
  --dataset dataset.jsonl `
  --manifest manifest.json `
  --out results.json
```

The ablation changes one thing only:

- `graph`: current `_activate()` with edges, decay, and hops;
- `no_spread`: the same nodes, threshold, top-k and body gate, but only direct query overlap.

Per case and algorithm, `results.json` records:

- `correct_injection`: expected skill body was injected, 0/1;
- `wrong_injection`: number of injected skill bodies outside `expected`;
- `abstain_error`: expected `[]` but a body was injected, 0/1;
- `injected_chars`: Python Unicode-character count of the complete recall block, including
  its header, provenance wrapper and top-k text, rendered in the same format as `recall()`.

The comparison uses an exact paired McNemar/binomial test. “Two extra wins” is not a
significance threshold: with two graph-only wins and zero losses, one-sided `p=0.25` and
two-sided `p=0.5`. With zero losses, significance at `alpha=0.05` requires five wins for a
predeclared one-sided test or six for a two-sided test.

The machine decision retains spreading activation only when:

1. graph has more correct injections;
2. exact one-sided paired `p < 0.05`;
3. wrong injections do not increase;
4. abstain errors do not increase.

Character count is reported as cost, not used to rescue a quality failure.
