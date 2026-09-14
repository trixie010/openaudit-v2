# Run context (inject into EVERY Category B / referee / fuzz-harness agent prompt)

Adapted from open-kritt built-in-variables (credit Blockian/Kritt-ai). No agent works without
this header - it pins scope and kills "the agent quietly analyzed a different target".

```
REPO: {{repo_full}}
COMMIT: {{commit_sha}}                      # every file:line citation is against THIS pin
SCOPE: {{repo_scope}}                       # copied from out/{slug}/SCOPE.md
IN-SCOPE FILES: out/{slug}/in-scope-files.txt
DEPENDENCIES: {{dependencies}}              # vendored libs + their pinned versions
CONFIGURATION: {{configuration}}             # compiler/flags from foundry.toml; live deployment facts
WORKSPACE: out/{slug}/fuzzing/target-work/  # or src/ - where to READ code
KB: out/{slug}/kb/{functions.csv, dependency-map.md, access-control.md, invariants.md, kritt.md}
```

Rules:
1. Citations MUST be `path:line` against the pinned commit - an agent that cites a file outside
   IN-SCOPE FILES is off-scope, not a finding.
2. The agent may NOT re-decide scope. Scope questions go to the operator with a recommendation.
3. For multi-repo targets: pass each repo's own pin (mirrors their workspace_manifest_json).
4. `{{extra.*}}` equivalent: run-specific operator notes ride in the same block, not scattered
   across prompts.
