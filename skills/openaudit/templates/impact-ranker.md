# Impact ranker (house rules) - composed with the program's payout table per run

# Composition rule (from open-kritt severity-ranker practice, method credit Blockian/Kritt-ai):
# final ranking rules = THIS FILE + the program's impact table quoted verbatim (SCOPE.md)
# + any run-specific rules the operator adds. Combined set must never be empty.

# Ranking rules

1. Findings whose impact matches a NAMED payable class on the program's table outrank
   everything else. Quote the program wording in the finding's impact_class field.
2. ROUTINE-OPERATION trigger rows outrank ATTACKER-DRIVEN rows at equal impact: the protocol's
   own normal operation reaching the bug is the strongest evidence it will actually be reached.
3. Direct theft / permanent freezing of FUNDS ranks next; freezing of UNCLAIMED YIELD next.
4. DoS/availability-only findings rank below any fund-affecting finding of the same target.
5. A finding that moves funds but lacks trigger_flow + malicious_input_example + file:line
   receipts is INCOMPLETE - rank it below complete findings, never discard it.
6. Downgrade: requires attacker to already hold a governance multisig or leaked keys
   (program-excluded class). Keep at original severity - flag for operator, never silent-drop.
7. Downgrade: test-only, deprecated, or not-deployed code paths (verify against the program's
   deployment scope list first).
8. Ambiguity (e.g. is the keeper bot a "privileged address" for the exclusion?) does NOT
   downgrade - tie-break UP and write the exact question for triage.
9. Within equal severity: rank by funds-at-risk, then by how many users/positions are affected.
10. Blind-first ordering: findings judged on their own evidence; known-issue/duplicate
    comparison happens ONLY at the confirm/report axes (dup_risk), never as a discovery input.
11. `needs-coverage`/`shallow` health is not "clean": an incomplete ledger or pending scopes
    outrank any Low/Info-only conclusion - the run must not be presented as a negative result.
12. Evidence-boundary raises priority ties broken by: reproduced > confirmed-differential >
    confirmed-executable > suspected, at equal impact class.
