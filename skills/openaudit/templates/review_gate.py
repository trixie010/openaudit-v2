#!/usr/bin/env python3
"""Pre-submission Review gate (Studio-Review analog).

Deterministic checks on candidates.csv before anything is promoted/submitted:
duplicates (submitted handoff, prior audits, disclosed corpus, intra-run,
institutional memory), program known issues, evidence completeness, and
impact-table payability.

It does NOT replace the fresh reviewer subagent - it hands that agent receipts.
Verdicts: ready | uncertain | high-risk | duplicate | known-issue.

Precision guard (learned from C4 replays): generic ERC20 hygiene (SafeERC20 /
ignored returns / fee-on-transfer / decimals), admin-config, and centralization
(e.g. pause) claims are auto-held at `uncertain` unless they show an unprivileged
impact path. Judges repeatedly reject these as QA even when README says such
tokens are in scope.
"""
import argparse
import csv
import fnmatch
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

_MEM = None
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import dismissctl as _MEM
    _DEFAULT_MEM = str(_MEM.default_db())
except Exception:  # memory helper optional
    _DEFAULT_MEM = str(Path.home() / ".openaudit" / "memory" / "findings.db")

STOP = set(
    "the a an and or of to in for on with by is are be this that it its as at from into when if "
    "then than which who whom whose can could should would may might must not no nor so such own "
    "same too very s t will just don now also only any all each per via one two use used using "
    "value values amount amounts function functions".split()
)


def log(*a):
    print(*a, file=sys.stderr)


def tokens(s):
    return [w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if w not in STOP]


def tfidf(docs):
    n = len(docs)
    df = Counter()
    tfs = []
    for d in docs:
        c = Counter(d)
        tfs.append(c)
        for w in c:
            df[w] += 1
    idf = {w: math.log((n + 1) / (df[w] + 1)) + 1.0 for w in df}
    vecs = []
    for c in tfs:
        v = {w: (1.0 + math.log(k)) * idf[w] for w, k in c.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({w: x / norm for w, x in v.items()})
    return vecs


def cos(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(w, 0.0) for w, x in a.items())


def norm_sig(s):
    return re.sub(r"\s+", " ", (s or "").strip()).replace("|", "/")


def norm_sev(raw):
    r = (raw or "").strip().lower()
    m = re.match(r"^\s*(critical|high|medium|med|low|info)\b", r)
    if m:
        t = m.group(1)
        return "Medium" if t == "med" else t.capitalize()
    for w in ("critical", "high", "medium", "low", "info"):
        if w in r:
            return w.capitalize()
    return "Unknown"


def load_candidates(path):
    with path.open(encoding="utf-8", errors="replace") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        title = (r.get("title") or "").strip()
        if not title:
            title = ((r.get("vulnerability_type") or "") + ": " + (r.get("summary") or "")[:120]).strip(": ").strip()
        out.append({
            "row": r,
            "id": r.get("id", ""),
            "title": title,
            "sev_claim": r.get("severity_claim", ""),
            "sev": norm_sev(r.get("referee_severity") or r.get("severity_claim", "")),
            "summary": r.get("summary", ""),
            "file": r.get("file", ""),
            "line": r.get("line", ""),
            "vuln_type": r.get("vulnerability_type", ""),
            "trigger": r.get("trigger_flow", ""),
            "input_example": r.get("malicious_input_example", ""),
            "dup_signature": norm_sig(r.get("dup_signature")),
            "status": (r.get("status") or "").strip(),
            "verdict": (r.get("referee_verdict") or "").strip(),
            "program_minimum": r.get("program_minimum", ""),
            "text": " ".join(filter(None, [
                title, r.get("summary", ""), r.get("vulnerability_type", ""),
                r.get("trigger_flow", ""), Path(r.get("file") or "").name])),
        })
    return out


def parse_md_table(path):
    if not path.exists():
        return []
    rows = []
    header = None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        if re.fullmatch(r"\|[\s:|-]+\|?", s) and "-" in s:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if header is None:
            header = cells
        else:
            rows.append({header[i] if i < len(header) else f"c{i}": cells[i] for i in range(len(cells))})
    return rows


def load_handoff(hdir):
    submitted, known = [], []
    if not hdir.exists():
        return submitted, known
    for row in parse_md_table(hdir / "submitted-findings.md"):
        text = " ".join(row.values())
        submitted.append({
            "id": row.get("id", ""),
            "dup_signature": norm_sig(row.get("dup_signature")),
            "text": text,
        })
    for row in parse_md_table(hdir / "known-issues.md"):
        known.append({"id": row.get("id", ""), "text": " ".join(row.values())})
    return submitted, known


def load_prior_chunks(pdir, min_len=40):
    chunks = []
    if not pdir.exists():
        return chunks
    for f in sorted(pdir.glob("*.md")):
        txt = f.read_text(encoding="utf-8", errors="replace")
        for part in re.split(r"\n(?=#{1,3} )", txt):
            part = part.strip()
            if len(part) >= min_len:
                chunks.append((f.name, part))
    return chunks


def corpus_hits(text, db_path, protocol_name, top=3):
    if not db_path.exists():
        return []
    corpus_scripts = Path(__file__).resolve().parents[2] / "disclosed-findings-corpus" / "scripts"
    sys.path.insert(0, str(corpus_scripts))
    try:
        from retrieval.index import FindingsIndex
        idx = FindingsIndex(db_path)
        hits = idx.dedup_search(text, protocol_name=protocol_name, top_k=top)
    except Exception as e:
        log(f"[review] corpus unavailable: {e}")
        return []
    docs = [tokens(text)] + [tokens(h.title + " " + h.root_cause) for h in hits]
    vecs = tfidf(docs) if docs else []
    out = []
    for i, h in enumerate(hits, 1):
        out.append({"ref": h.title, "severity": h.severity, "sim": cos(vecs[0], vecs[i])})
    return sorted(out, key=lambda x: -x["sim"])


def check_impact(c, ruleset):
    reasons = []
    if not ruleset:
        return True, ["impact-ruleset.md missing; impact checked manually"]
    for m in re.finditer(r"([\w./*-]+)\s*=\s*ONLY\s+Critical", ruleset, re.I):
        pat = m.group(1)
        if c["file"] and (c["file"].startswith(pat.rstrip("*")) or fnmatch.fnmatch(c["file"], pat)):
            if c["sev"] != "Critical":
                reasons.append(f"domain {pat} accepts ONLY Critical; claim={c['sev']}")
    if c["sev"] in ("Low", "Info") and re.search(r"Low/Info\s*:\s*out of scope", ruleset, re.I):
        reasons.append("Low/Info declared out of scope by the program")
    return (not reasons), reasons


_GENERIC_HYGIENE = re.compile(
    r"safeerc20|unchecked.erc20|return value|returns? false|fee.on.transfer|"
    r"rebasing|non.standard|decimals?\b|18\s*-\s*decimals|transfer\(\)|2300",
    re.I,
)
_CENTRAL = re.compile(
    r"\bpaused?\b|onlyadmin|admin[- ]?(config|misconfig)|misconfiguration|"
    r"centraliz|privileged role|owner can",
    re.I,
)
_UNPRIV = re.compile(
    r"steal|theft|drain|insolven|anyone can|permissionless|unprivileged|any user",
    re.I,
)


def check_quality(c):
    hay = " ".join([
        c.get("title", ""), c.get("summary", ""), c.get("trigger", ""),
        c.get("input_example", ""), c.get("vuln_type", ""),
    ])
    reasons = []
    if _GENERIC_HYGIENE.search(hay):
        reasons.append(
            "generic-hygiene claim (token/config robustness) - needs an unprivileged "
            "theft/insolvency PoC before it is submission-grade")
    elif _CENTRAL.search(hay) and not _UNPRIV.search(hay):
        reasons.append(
            "admin-config/centralization claim - needs an unprivileged impact path")
    return reasons


def is_poc_backed(status, poc_status):
    st = (status or "").strip().lower()
    poc = (poc_status or "").strip().lower()
    if poc.startswith("not") or poc in ("", "none", "no", "n/a", "pending", "suspected"):
        return False
    markers = ("executable", "differential", "written", "passing", "verified", "poc", "tested")
    return any(m in poc for m in markers) or any(m in st for m in markers)


_FORK_RE = re.compile(r"\bfork\b", re.I)


def check_fork(c, required):
    if not required:
        return []
    eb = (c["row"].get("evidence_boundary") or "").strip()
    if _FORK_RE.search(eb):
        return []
    return ["fork evidence (evidence_boundary must be fork:<chain>@<block>, not local-run/static)"]


def check_evidence(c):
    missing = []
    if not c["file"] or not str(c["line"]).strip():
        missing.append("file:line")
    if not (c["trigger"] or c["input_example"]):
        missing.append("trigger path / malicious input")
    confirmed = c["status"].lower().startswith("confirmed") or c["verdict"].upper() == "CONFIRMED"
    poc_backed = is_poc_backed(c["status"], (c["row"].get("poc_status") or ""))
    if not (confirmed and poc_backed):
        missing.append("confirmed + PoC-backed (status=confirmed-* and poc_status=written/passing/executable)")
    return missing


def main():
    ap = argparse.ArgumentParser(description="Pre-submission Review gate for openaudit candidates.csv")
    ap.add_argument("--candidates", default=None, help="default: out/{slug}/candidates.csv")
    ap.add_argument("--slug", default=None)
    ap.add_argument("--handoff", default=None, help="default: out/{slug}/handoff/")
    ap.add_argument("--prior-audits", default=None, help="default: out/{slug}/prior-audits/")
    ap.add_argument("--impact-ruleset", default=None, help="default: out/{slug}/kb/impact-ruleset.md")
    ap.add_argument("--corpus-db", default=str(
        Path(__file__).resolve().parents[2] / "disclosed-findings-corpus" / "data" / "findings.db"))
    ap.add_argument("--protocol-name", default=None,
                    help="corpus dedup filter (protocol-name substring, e.g. Tornado)")
    ap.add_argument("--memory-db", default=_DEFAULT_MEM, help="institutional memory SQLite (empty disables)")
    ap.add_argument("--out", default=None, help="default: out/{slug}/review/")
    ap.add_argument("--dup-threshold", type=float, default=0.55)
    ap.add_argument("--known-threshold", type=float, default=0.50)
    ap.add_argument("--intra-threshold", type=float, default=0.85)
    ap.add_argument("--require-fork", action="store_true",
                    help="address targets: require evidence_boundary=fork:<chain>@<block>")
    ap.add_argument("--fail-on-blocker", action="store_true")
    args = ap.parse_args()

    if not args.candidates:
        if not args.slug:
            log("need --candidates or --slug")
            return 1
        args.candidates = f"out/{args.slug}/candidates.csv"
    cand_path = Path(args.candidates)
    if not cand_path.exists():
        log(f"candidates not found: {cand_path}")
        return 1

    slug = args.slug or cand_path.parent.name
    base = cand_path.parent
    handoff_dir = Path(args.handoff) if args.handoff else base / "handoff"
    prior_dir = Path(args.prior_audits) if args.prior_audits else base / "prior-audits"
    ruleset_p = Path(args.impact_ruleset) if args.impact_ruleset else base / "kb" / "impact-ruleset.md"
    out_dir = Path(args.out) if args.out else base / "review"
    out_dir.mkdir(parents=True, exist_ok=True)

    cands = load_candidates(cand_path)
    submitted, known = load_handoff(handoff_dir)
    prior = load_prior_chunks(prior_dir)
    ruleset = ruleset_p.read_text(encoding="utf-8", errors="replace") if ruleset_p.exists() else ""

    pool = [c["text"] for c in cands] + [s["text"] for s in submitted] + \
           [k["text"] for k in known] + [ch for _, ch in prior]
    vecs = tfidf([tokens(t) for t in pool]) if pool else []
    n_c = len(cands)
    n_s = len(submitted)
    n_k = len(known)
    prior_vecs = vecs[n_c + n_s + n_k:]

    sub_sigs = {s["dup_signature"] for s in submitted if s["dup_signature"]}
    intra = {}
    for i in range(n_c):
        for j in range(i):
            if cands[i]["dup_signature"] and cands[i]["dup_signature"] == cands[j]["dup_signature"]:
                intra.setdefault(i, (j, 1.0))
            elif vecs and cos(vecs[i], vecs[j]) >= args.intra_threshold:
                intra.setdefault(i, (j, cos(vecs[i], vecs[j])))

    results = []
    for i, c in enumerate(cands):
        reasons, fixes = [], []
        dup_refs = []

        if c["dup_signature"] and c["dup_signature"] in sub_sigs:
            dup_refs.append(f"submitted dup_signature={c['dup_signature']}")
        if n_s and vecs:
            best = max(range(n_s), key=lambda j: cos(vecs[i], vecs[n_c + j]))
            s = cos(vecs[i], vecs[n_c + best])
            if s >= args.dup_threshold:
                dup_refs.append(f"handoff submitted {submitted[best]['id'] or '?'} (sim {s:.2f})")
        if prior_vecs:
            best_p = max(range(len(prior_vecs)), key=lambda j: cos(vecs[i], prior_vecs[j]))
            sp = cos(vecs[i], prior_vecs[best_p])
            if sp >= args.dup_threshold:
                dup_refs.append(f"prior audit {prior[best_p][0]} (sim {sp:.2f})")
        chits = corpus_hits(c["text"], Path(args.corpus_db), args.protocol_name)
        for h in chits:
            if h["sim"] >= args.dup_threshold:
                dup_refs.append(f"corpus: {h['ref']} ({h['severity']}, sim {h['sim']:.2f})")
        if i in intra:
            j, s = intra[i]
            dup_refs.append(f"intra-run duplicate of {cands[j]['id']} (sim {s:.2f})")

        known_refs = []
        if n_k and vecs:
            best_k = max(range(n_k), key=lambda j: cos(vecs[i], vecs[n_c + n_s + j]))
            sk = cos(vecs[i], vecs[n_c + n_s + best_k])
            if sk >= args.known_threshold:
                known_refs.append(f"known issue {known[best_k]['id'] or '?'} (sim {sk:.2f})")

        if _MEM is not None and args.memory_db:
            try:
                hits = _MEM.memory_matches(
                    args.memory_db, slug, c["text"], c["dup_signature"],
                    c["file"], c.get("vuln_type", ""),
                    min_score=args.known_threshold, limit=3)
            except Exception as e:  # noqa: BLE001 - memory must never block the gate
                log(f"memory query failed: {e}")
                hits = []
            for h in hits:
                ref = f"memory {h['verdict']} #{h['id']} '{h['title'][:40]}' (sim {h['score']:.2f})"
                if h["verdict"] in ("duplicate", "accepted"):
                    dup_refs.append(ref)
                else:
                    known_refs.append(ref)

        ev_missing = check_evidence(c) + check_fork(c, args.require_fork)
        impact_ok, impact_reasons = check_impact(c, ruleset)
        quality_reasons = check_quality(c)

        if dup_refs:
            verdict = "duplicate"
            reasons += dup_refs
            fixes.append("drop, or reframe only if root cause differs - cite the differing mechanism")
        elif known_refs:
            verdict = "known-issue"
            reasons += known_refs
            fixes.append("do not submit; map to the project's stated known issue")
        elif not impact_ok:
            verdict = "high-risk"
            reasons += impact_reasons
            fixes.append("re-map to a payable impact class or drop")
        elif quality_reasons:
            verdict = "uncertain"
            reasons += quality_reasons
            fixes.append("show an unprivileged impact/PoC or drop (generic hygiene/admin-config)")
        elif ev_missing:
            hard = ("file:line", "trigger path / malicious input", "fork evidence")
            if any(any(k in m for k in hard) for m in ev_missing):
                verdict = "high-risk"
            else:
                verdict = "uncertain"
            reasons += [f"missing: {m}" for m in ev_missing]
            fixes.append("add the missing receipts before submitting")
        else:
            verdict = "ready"

        results.append({
            "id": c["id"], "title": c["title"], "severity": c["sev"],
            "verdict": verdict, "reasons": reasons, "fixes": fixes,
            "evidence_missing": ev_missing, "impact_ok": impact_ok,
            "corpus": chits, "file": c["file"], "line": c["line"],
            "vuln_type": c.get("vuln_type", ""),
            "dup_signature": c["dup_signature"], "summary": c["summary"],
        })

    with (out_dir / "review.jsonl").open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    counts = Counter(r["verdict"] for r in results)
    lines = [f"# Submission review - {slug}", ""]
    lines.append(f"Candidates: {len(results)}  |  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    lines.append("")
    lines.append("| ID | Sev | Verdict | Title | Why |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        why = "; ".join(r["reasons"][:2]) or "-"
        lines.append(f"| {r['id']} | {r['severity']} | **{r['verdict']}** | {r['title'][:70]} | {why[:110]} |")
    lines.append("")
    for verdict in ("duplicate", "known-issue", "high-risk", "uncertain", "ready"):
        group = [r for r in results if r["verdict"] == verdict]
        if not group:
            continue
        lines.append(f"## {verdict} ({len(group)})")
        lines.append("")
        for r in group:
            lines.append(f"- **{r['id']}** ({r['severity']}): {r['title']}")
            for x in r["reasons"]:
                lines.append(f"    - {x}")
            for x in r["fixes"]:
                lines.append(f"    - fix: {x}")
        lines.append("")
    (out_dir / "review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    log(f"[review] {dict(counts)} -> {out_dir/'review.md'}")
    blockers = counts.get("duplicate", 0) + counts.get("high-risk", 0)
    if args.fail_on_blocker and blockers:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
