#!/usr/bin/env python3
import argparse
import csv
import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ORG = "code-423n4"
DEFAULT_OUT = Path.home() / "openaudit" / "out"
DEFAULT_RUNS = Path.home() / "openaudit" / "eval-runs"
SEV_HM = ("Critical", "High", "Medium")
STOP = set(
    "the a an and or of to in for on with by is are be this that it its as at from into when if "
    "then than which who whom whose can could should would may might must not no nor so such own "
    "same too very s t will just don now also only any all each per via one two use used using "
    "value values amount amounts function functions".split()
)


def log(*a):
    print(*a, file=sys.stderr)


def slug_base(s):
    s = s.strip().strip("/")
    if s.endswith("-findings"):
        s = s[: -len("-findings")]
    return s


def workdir_for(args):
    return Path(args.workdir).expanduser() if args.workdir else DEFAULT_RUNS / slug_base(args.slug)


def run_git_clone(url, dest):
    if dest.exists():
        log(f"[fetch] reuse existing clone: {dest}")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"[fetch] git clone {url} -> {dest}")
    r = subprocess.run(
        ["git", "clone", "--depth", "1", "--single-branch", url, str(dest)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        log(f"[fetch] clone FAILED: {r.stderr.strip()[:400]}")
        return False
    return True


def parse_front_matter(text):
    fm = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip().strip('"').strip("'")
    return fm


FIND_RE = re.compile(r"\[\s*\[?\s*([HM])-(\d+)\s*\]")
URL_RE = re.compile(r"\((https?://[^)]+)\)\s*$")


def parse_report(md_path):
    text = md_path.read_text(encoding="utf-8", errors="replace")
    fm = parse_front_matter(text)
    sections = []
    cur = None
    for line in text.splitlines():
        if line.startswith("## "):
            m = FIND_RE.search(line)
            if not m:
                cur = None
                continue
            sev = "High" if m.group(1) == "H" else "Medium"
            num = int(m.group(2))
            raw = line[3:].strip()
            url_m = URL_RE.search(raw)
            url = url_m.group(1) if url_m else ""
            title = FIND_RE.sub("", raw, count=1).strip().lstrip("]").strip()
            if url_m:
                title = title[: title.rfind("]")].strip() if "]" in title else title
            cur = {"id": f"{m.group(1)}-{num:02d}", "severity": sev,
                   "title": title, "issue_url": url, "body": []}
            sections.append(cur)
            continue
        if cur is not None:
            cur["body"].append(line)
    for s in sections:
        s["body"] = "\n".join(s["body"]).strip()
    return fm, sections


RISK_MAP = {"3": "High", "2": "Medium", "1": "Low", "Q": "QA"}


def parse_data(data_dir):
    out = []
    if not data_dir.exists():
        return out
    for f in sorted(data_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        out.append({
            "risk": RISK_MAP.get(str(d.get("risk")), str(d.get("risk"))),
            "risk_raw": str(d.get("risk")),
            "title": d.get("title", ""),
            "issueId": str(d.get("issueId", "")),
            "issueUrl": d.get("issueUrl", ""),
            "originalIssueId": str(d.get("originalIssueId", "")),
            "handle": d.get("handle", ""),
        })
    return out


def cmd_fetch(args):
    slug = slug_base(args.slug)
    wd = workdir_for(args)
    ak = wd / "_answer_key"
    ak.mkdir(parents=True, exist_ok=True)

    if args.findings_src:
        findings = Path(args.findings_src).expanduser()
        if not findings.exists():
            log(f"[fetch] --findings-src not found: {findings}")
            return 1
    else:
        findings = ak / "findings"
        if not run_git_clone(f"https://github.com/{ORG}/{slug}-findings.git", findings):
            return 1

    report = None
    for name in ("report.md", "README.md"):
        if (findings / name).exists():
            report = findings / name
            break
    if report is None:
        log(f"[fetch] no report.md/README.md in {findings}")
        return 1

    fm, sections = parse_report(report)
    data = parse_data(findings / "data")
    by_issue = {r["issueId"]: r for r in data if r["issueId"]}

    gt = []
    for s in sections:
        rec = dict(s)
        rec["source"] = report.name
        m = re.search(r"/issues/(\d+)", s.get("issue_url", ""))
        if m:
            did = m.group(1)
            rec["issue_id"] = did
            d = by_issue.get(did)
            if d:
                rec["original_issue"] = d["originalIssueId"] or did
                rec["dup_handle"] = d["handle"]
        gt.append(rec)

    with (ak / "ground_truth.jsonl").open("w", encoding="utf-8") as fh:
        for rec in gt:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    hm = [g for g in gt if g["severity"] in ("High", "Medium")]
    sub_hm = [r for r in data if r["risk"] in ("High", "Medium")]
    roots = Counter(r["originalIssueId"] or r["issueId"] for r in sub_hm)
    meta = {
        "slug": slug,
        "title": fm.get("title", ""),
        "date": fm.get("date", ""),
        "contest": fm.get("contest", ""),
        "findings_url": fm.get("findings", ""),
        "findings_source": str(findings),
        "gt_counts": dict(Counter(g["severity"] for g in hm)),
        "gt_total": len(hm),
        "submission_counts": dict(Counter(r["risk"] for r in sub_hm)),
        "submission_total": len(sub_hm),
        "unique_roots": len(roots),
        "duplicate_submissions": max(0, len(sub_hm) - len(roots)),
    }

    if not args.skip_target:
        if args.target_src:
            tsrc = Path(args.target_src).expanduser()
            tdir = wd / "target"
            if tsrc.exists() and not tdir.exists():
                try:
                    os.symlink(tsrc, tdir)
                except OSError:
                    tdir = tsrc
            else:
                tdir = tdir if tdir.exists() else tsrc
        else:
            tdir = wd / "target"
            if not run_git_clone(f"https://github.com/{ORG}/{slug}.git", tdir):
                meta["target_error"] = "clone failed"
                tdir = None
        if tdir and (Path(tdir) / ".git").exists():
            r = subprocess.run(["git", "-C", str(tdir), "rev-parse", "HEAD"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                meta["target_sha"] = r.stdout.strip()
        if tdir:
            meta["target_path"] = str(tdir)
        meta["scope_txt"] = str(Path(tdir) / "scope.txt") if tdir and (Path(tdir) / "scope.txt").exists() else ""

    (ak / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log("[fetch] answer key written")
    log(f"[fetch] GT H/M: {meta['gt_counts']} total={meta['gt_total']}  "
        f"submissions={meta['submission_counts']} unique_roots={meta['unique_roots']} "
        f"dups={meta['duplicate_submissions']}")
    if meta.get("target_path"):
        log(f"[fetch] target: {meta['target_path']} @ {meta.get('target_sha','?')}")
    return 0


def norm_sev(raw):
    r = (raw or "").strip().lower()
    m = re.match(r"^\s*(critical|high|medium|med|low|info)\b", r)
    if m:
        t = m.group(1)
        return "Medium" if t == "med" else t.capitalize()
    if "critical" in r:
        return "Critical"
    if "high" in r:
        return "High"
    if "medium" in r or re.search(r"\bmed\b", r):
        return "Medium"
    if "low" in r:
        return "Low"
    if "info" in r:
        return "Info"
    return "Unknown"


def cmd_parse(args):
    slug = slug_base(args.slug)
    wd = workdir_for(args)
    if args.candidates:
        cand = Path(args.candidates).expanduser()
    else:
        cand = DEFAULT_OUT / slug / "candidates.csv"
        if not cand.exists():
            cand = DEFAULT_OUT / args.slug / "candidates.csv"
    if not cand.exists():
        log(f"[parse] candidates.csv not found: {cand} (pass --candidates)")
        return 1

    with cand.open(encoding="utf-8", errors="replace") as fh:
        rows = list(csv.DictReader(fh))

    produced = []
    for r in rows:
        sev_raw = (r.get("referee_severity") or r.get("severity_claim") or "").strip()
        sev = norm_sev(sev_raw)
        title = (r.get("title") or "").strip()
        if not title:
            vt = (r.get("vulnerability_type") or "").strip()
            sm = (r.get("summary") or "").strip()
            title = (vt + ": " + sm[:120]).strip(": ").strip()
        status = (r.get("status") or "").strip()
        verdict = (r.get("referee_verdict") or "").strip()
        submitted = status.lower().startswith("confirmed") or verdict.upper() == "CONFIRMED"
        fname = Path(r.get("file") or "").name
        text = " ".join(x for x in (title, r.get("summary", ""), r.get("trigger_flow", ""), fname) if x)
        produced.append({
            "id": r.get("id", ""),
            "severity": sev,
            "severity_raw": sev_raw,
            "title": title,
            "summary": r.get("summary", ""),
            "file": r.get("file", ""),
            "line": r.get("line", ""),
            "status": status,
            "referee_verdict": verdict,
            "submitted": submitted,
            "text": text,
        })

    with (wd / "produced.jsonl").open("w", encoding="utf-8") as fh:
        for rec in produced:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    hm = [p for p in produced if p["severity"] in SEV_HM]
    log(f"[parse] {len(produced)} rows -> {len(hm)} H/M  "
        f"sev={dict(Counter(p['severity'] for p in produced))}  "
        f"submitted={sum(1 for p in hm if p['submitted'])}")
    return 0


def tok(s):
    return [w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if w not in STOP]


def build_vectors(docs):
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


def judge(cmd, produced, truth, sim):
    payload = json.dumps({"produced": produced, "truth": truth, "similarity": round(sim, 4)})
    try:
        r = subprocess.run(cmd, shell=True, input=payload, capture_output=True, text=True, timeout=180)
        m = re.search(r"\{.*\}", r.stdout, re.S)
        if not m:
            return None
        return bool(json.loads(m.group(0)).get("match"))
    except Exception as e:
        log(f"[judge] error: {e}")
        return None


def load_jsonl(p):
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def cmd_score(args):
    slug = slug_base(args.slug)
    wd = workdir_for(args)
    gt = [g for g in load_jsonl(wd / "_answer_key" / "ground_truth.jsonl")
          if g.get("severity") in ("High", "Medium")]
    produced = load_jsonl(wd / "produced.jsonl")
    meta = {}
    mp = wd / "_answer_key" / "meta.json"
    if mp.exists():
        meta = json.loads(mp.read_text())
    if not gt:
        log("[score] empty ground truth (run fetch first)")
        return 1
    if not produced:
        log("[score] empty produced.jsonl (run parse first)")
        return 1

    gt_docs = [tok((g.get("title", "") + " " + g.get("body", ""))) for g in gt]
    p_docs = [tok(p.get("text", "")) for p in produced]
    vecs = build_vectors(gt_docs + p_docs)
    gv = vecs[: len(gt_docs)]
    pv = vecs[len(gt_docs):]

    aligned = []
    lowconf = []
    for i, p in enumerate(produced):
        sims = sorted(((cos(pv[i], gv[j]), j) for j in range(len(gt))), reverse=True)
        best_sim, best_j = sims[0]
        ok = best_sim >= args.threshold
        if not ok and best_sim >= args.judge_floor and args.judge_cmd:
            if judge(args.judge_cmd, p, gt[best_j], best_sim) is True:
                ok = True
        if ok:
            aligned.append((i, best_j, best_sim))
        elif best_sim >= args.judge_floor:
            lowconf.append((i, best_j, best_sim))

    matched_all = {j for _, j, _ in aligned}
    matched_conf = {j for i, j, _ in aligned if produced[i]["submitted"]}
    hm_idx = [i for i, p in enumerate(produced) if p["severity"] in SEV_HM]
    sub_idx = [i for i in hm_idx if produced[i]["submitted"]]
    aligned_sub = {i for i, _, _ in aligned if produced[i]["submitted"]}
    per_gt = Counter(j for _, j, _ in aligned)
    dups = [(j, per_gt[j]) for j in per_gt if per_gt[j] > 1]

    def recall(idx):
        return (sum(1 for j in idx if j in matched_all) / len(idx)) if idx else None

    def recall_conf(idx):
        return (sum(1 for j in idx if j in matched_conf) / len(idx)) if idx else None

    high_idx = [j for j, g in enumerate(gt) if g["severity"] == "High"]
    med_idx = [j for j, g in enumerate(gt) if g["severity"] == "Medium"]
    fp = [produced[i] for i in sub_idx if i not in aligned_sub]

    score = {
        "slug": slug,
        "gt_total": len(gt),
        "gt_counts": dict(Counter(g["severity"] for g in gt)),
        "produced_total": len(produced),
        "produced_hm": len(hm_idx),
        "produced_submitted": len(sub_idx),
        "matched_gt_all": len(matched_all),
        "matched_gt_confirmed": len(matched_conf),
        "recall_all": recall(list(range(len(gt)))),
        "recall_confirmed": recall_conf(list(range(len(gt)))),
        "recall_high": recall(high_idx),
        "recall_medium": recall(med_idx),
        "precision_submitted": (len(aligned_sub) / len(sub_idx)) if sub_idx else None,
        "false_positives": len(fp),
        "duplicates": sum(c - 1 for _, c in dups),
        "threshold": args.threshold,
        "judge_floor": args.judge_floor,
        "judge_used": bool(args.judge_cmd),
        "meta": meta,
    }
    (wd / "score.json").write_text(json.dumps(score, indent=2), encoding="utf-8")

    def pct(x):
        return "n/a" if x is None else f"{x*100:.0f}%"

    lines = []
    lines.append(f"# Eval scorecard - {slug}")
    lines.append("")
    if meta.get("title"):
        lines.append(f"**{meta['title']}**  ({meta.get('date','')}, contest {meta.get('contest','')})")
        lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Answer key (unique H/M) | {score['gt_counts']} total={score['gt_total']} |")
    lines.append(f"| Produced H/M | {score['produced_hm']} (submitted {score['produced_submitted']}) |")
    lines.append(f"| Recall - all | {pct(score['recall_all'])} ({score['matched_gt_all']}/{score['gt_total']}) |")
    lines.append(f"| Recall - confirmed | {pct(score['recall_confirmed'])} ({score['matched_gt_confirmed']}/{score['gt_total']}) |")
    lines.append(f"| Recall - High | {pct(score['recall_high'])} |")
    lines.append(f"| Recall - Medium | {pct(score['recall_medium'])} |")
    lines.append(f"| Precision - submitted | {pct(score['precision_submitted'])} |")
    lines.append(f"| False positives (submitted) | {score['false_positives']} |")
    lines.append(f"| Duplicate submissions | {score['duplicates']} |")
    lines.append("")

    missed = [g for j, g in enumerate(gt) if j not in matched_all]
    lines.append(f"## Missed answer-key findings ({len(missed)})")
    lines.append("")
    if missed:
        lines.append("| ID | Sev | Title |")
        lines.append("|---|---|---|")
        for g in missed:
            lines.append(f"| {g.get('id','')} | {g['severity']} | {g['title']} |")
    else:
        lines.append("_none - full recall_")
    lines.append("")

    lines.append(f"## False positives (submitted but unmatched) ({len(fp)})")
    lines.append("")
    if fp:
        lines.append("| ID | Sev claimed | Title |")
        lines.append("|---|---|---|")
        for p in fp:
            lines.append(f"| {p['id']} | {p['severity']} | {p['title'][:110]} |")
    else:
        lines.append("_none_")
    lines.append("")

    lines.append(f"## Duplicates ({score['duplicates']})")
    lines.append("")
    if dups:
        lines.append("| Answer-key ID | Title | Produced rows |")
        lines.append("|---|---|---|")
        for j, c in sorted(dups, key=lambda x: -x[1]):
            ids = ", ".join(produced[i]["id"] for i, jj, _ in aligned if jj == j)
            lines.append(f"| {gt[j].get('id','')} | {gt[j]['title'][:80]} | {c} ({ids}) |")
    else:
        lines.append("_none_")
    lines.append("")

    if lowconf:
        lines.append(f"## Low-confidence pairs to review (sim in [{args.judge_floor}, {args.threshold}))")
        lines.append("")
        lines.append("| Produced | Answer-key | sim |")
        lines.append("|---|---|---|")
        for i, j, s in sorted(lowconf, key=lambda x: -x[2])[:20]:
            lines.append(f"| {produced[i]['id']}: {produced[i]['title'][:60]} | "
                         f"{gt[j].get('id','')}: {gt[j]['title'][:60]} | {s:.2f} |")
        lines.append("")

    (wd / "scorecard.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"[score] recall_all={pct(score['recall_all'])} recall_confirmed={pct(score['recall_confirmed'])} "
        f"precision_submitted={pct(score['precision_submitted'])} dups={score['duplicates']} "
        f"fp={score['false_positives']}")
    log(f"[score] wrote {wd/'score.json'} and {wd/'scorecard.md'}")
    return 0


def cmd_blind(args):
    slug = slug_base(args.slug)
    wd = workdir_for(args)
    meta_p = wd / "_answer_key" / "meta.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    target = meta.get("target_path") or str(wd / "target")
    scope = meta.get("scope_txt") or ""
    brief = [
        f"# BLIND eval brief - {slug}",
        "",
        f"Target source: {target}",
        f"Commit: {meta.get('target_sha','(unknown - clone with evalctl fetch)')}",
        f"Scope file: {scope or '(none found; use README scope table)'}",
        "",
        "## Rules (graded blind run)",
        "1. Audit ONLY the target above; citations are path:line against its commit.",
        f"2. Do NOT read {wd}/_answer_key, any *-findings repo, or any C4 report for this contest.",
        "3. Do not search the web for this contest's published findings.",
        "4. Write findings to out/<run-slug>/candidates.csv (skills/openaudit/templates/candidates.sample.csv schema).",
        "5. Goal: maximize unique H/M recall while minimizing duplicate submissions and false positives.",
        "",
        f"Suggested: /openaudit {target} <Project> --mode audit",
        "",
        "After the run: `evalctl.py parse` then `evalctl.py score`.",
    ]
    (wd / "BLIND_BRIEF.md").write_text("\n".join(brief) + "\n", encoding="utf-8")
    print("\n".join(brief))
    return 0


def main():
    ap = argparse.ArgumentParser(prog="evalctl.py", description="Blind eval harness for openaudit vs past C4 contests")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="clone answer key + target, write ground truth")
    f.add_argument("slug")
    f.add_argument("--workdir")
    f.add_argument("--findings-src", help="reuse a local <slug>-findings clone")
    f.add_argument("--target-src", help="reuse a local contest source clone")
    f.add_argument("--skip-target", action="store_true")
    f.set_defaults(func=cmd_fetch)

    p = sub.add_parser("parse", help="normalize out/<slug>/candidates.csv -> produced.jsonl")
    p.add_argument("slug")
    p.add_argument("--workdir")
    p.add_argument("--candidates")
    p.set_defaults(func=cmd_parse)

    s = sub.add_parser("score", help="match produced vs ground truth, write scorecard")
    s.add_argument("slug")
    s.add_argument("--workdir")
    s.add_argument("--threshold", type=float, default=0.34)
    s.add_argument("--judge-floor", type=float, default=0.18)
    s.add_argument("--topk", type=int, default=5)
    s.add_argument("--judge-cmd", help="shell command: stdin JSON pair -> stdout {match,reason}")
    s.set_defaults(func=cmd_score)

    b = sub.add_parser("blind", help="write/print the blind brief")
    b.add_argument("slug")
    b.add_argument("--workdir")
    b.set_defaults(func=cmd_blind)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
