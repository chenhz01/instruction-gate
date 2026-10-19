# -*- coding: utf-8 -*-
"""instruction-gate: system-prompt internal contradiction audit.

Defensive, offline text-rule analysis only — zero network, zero model calls.
Parses numbered/bulleted rules from a prompt and flags: direct positive/negative
conflicts on the same subject, duplicate rules, undeclared priority between
rules sharing a subject, circular override chains ("Rule N overrides Rule M"),
and vacuous rules with no analyzable content.
"""
import argparse
import json
import re
import sys

__version__ = "0.1.1"

RULE_LINE_RE = re.compile(r"^\s*(?:(\d+)[.)]\s+|[-*•]\s+)(.+?)\s*$", re.M)
NEG_RE = re.compile(
    r"\b(?:never|no|not|don't|dont|cannot|can't|cant|avoid|without|forbid)\b", re.I)
POS_RE = re.compile(r"\b(?:always|must|shall|required|ensure|prefer|use|keep)\b", re.I)
PRIORITY_RE = re.compile(
    r"\b(?:precedence|overrides?|takes\s+priority|instead\s+of|wins|trumps)\b", re.I)
OVERRIDE_RE = re.compile(
    r"rule\s*(\d+)\s+(?:overrides?|takes\s+precedence\s+over|precedes)\s+rule\s*(\d+)",
    re.I)

STOPWORDS = frozenset("""
the a an and or of to in on for with must shall should may can could will
would be is are was were been have has had do does did that this these those
it its as by at from into if then when while all any some each every no nor
not only own same so than too very you your yours we our us they them their
his her he she always never avoid dont don cannot cant without instead please
use using kept keep ensure prefer required requirement rule rules
""".split())


def parse_rules(text):
    """Return [{'num': explicit int|None, 'text': str}] from numbered/bulleted lines."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    rules = []
    for mt in RULE_LINE_RE.finditer(text):
        rules.append({"num": int(mt.group(1)) if mt.group(1) else None,
                      "text": mt.group(2).strip()})
    return rules


def keywords(rule_text):
    """Unicode-safe content keywords (len>=3, stopwords removed)."""
    toks = re.findall(r"[^\W\d_]{3,}", rule_text.lower(), re.UNICODE)
    return {t for t in toks if t not in STOPWORDS}


def modality(rule_text):
    """'negative' | 'positive' | 'neutral' (negative wins when both match)."""
    if NEG_RE.search(rule_text):
        return "negative"
    if POS_RE.search(rule_text):
        return "positive"
    return "neutral"


def _num_index_map(rules):
    num_map = {}
    for i, r in enumerate(rules):
        if r["num"] is not None and r["num"] not in num_map:
            num_map[r["num"]] = i
    return num_map


def _resolve(n, rules, num_map):
    if n in num_map:
        return num_map[n]
    if 1 <= n <= len(rules):
        return n - 1
    return None


def _find_cycle(edges):
    for start in edges:
        path, on_path = [], set()
        cur = start
        while cur is not None and cur in edges and cur not in on_path:
            on_path.add(cur)
            path.append(cur)
            cur = next(iter(edges[cur]), None)
        if cur is not None and cur in on_path:
            return path[path.index(cur):] + [cur]
    return None


def analyze_rules(text, min_shared=2):
    """Analyze a prompt; return finding dicts. Raises TypeError on non-str."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    findings = []

    def add(code, sev, msg, loc):
        findings.append({"code": code, "severity": sev, "message": msg, "location": loc})

    rules = parse_rules(text)
    if not rules:
        add("NO_RULES", "INFO", "no numbered/bulleted rules detected", "$")
        return findings

    kws = [keywords(r["text"]) for r in rules]
    mods = [modality(r["text"]) for r in rules]
    norm = [re.sub(r"\s+", " ", r["text"].strip().lower()) for r in rules]

    for i in range(len(rules)):
        for j in range(i + 1, len(rules)):
            pair = "rule %d / rule %d" % (i + 1, j + 1)
            if norm[i] == norm[j]:
                add("DUPLICATE_RULE", "WARN",
                    "rules %d and %d are identical" % (i + 1, j + 1), pair)
                continue
            inter = kws[i] & kws[j]
            if {mods[i], mods[j]} == {"positive", "negative"} and inter:
                add("DIRECT_CONFLICT", "ERROR",
                    "rules %d and %d conflict on %r (%s vs %s)"
                    % (i + 1, j + 1, sorted(inter), mods[i], mods[j]), pair)
            elif len(inter) >= min_shared:
                if not PRIORITY_RE.search(rules[i]["text"]) and \
                        not PRIORITY_RE.search(rules[j]["text"]):
                    add("UNDECLARED_PRIORITY", "INFO",
                        "rules %d and %d share subject %r but neither declares priority"
                        % (i + 1, j + 1, sorted(inter)), pair)

    num_map = _num_index_map(rules)
    edges = {}
    for mt in OVERRIDE_RE.finditer(text):
        src = _resolve(int(mt.group(1)), rules, num_map)
        dst = _resolve(int(mt.group(2)), rules, num_map)
        if src is None or dst is None:
            add("UNKNOWN_RULE_REF", "INFO",
                "override statement references unknown rule number(s): %s -> %s"
                % (mt.group(1), mt.group(2)), "$")
            continue
        edges.setdefault(src, set()).add(dst)
        if src == dst:
            add("CIRCULAR_OVERRIDE", "ERROR",
                "rule %d overrides itself" % (src + 1), "rule %d" % (src + 1))
    cycle = _find_cycle(edges)
    if cycle:
        chain = " -> ".join("rule %d" % (n + 1) for n in cycle)
        add("CIRCULAR_OVERRIDE", "ERROR",
            "circular override chain detected: %s" % chain, "$")

    for i, r in enumerate(rules):
        if not kws[i]:
            add("VAGUE_RULE", "INFO",
                "rule %d has no analyzable content keywords" % (i + 1),
                "rule %d" % (i + 1))
    return findings


def summarize(findings):
    out = {"error": 0, "warn": 0, "info": 0}
    for f in findings:
        key = f["severity"].lower()
        out[key] = out.get(key, 0) + 1
    return out


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="instruction-gate",
        description="Detect internal contradictions in system prompts: "
                    "positive/negative rule conflicts, duplicates, undeclared "
                    "priority, and circular overrides. Offline text analysis only.")
    p.add_argument("prompt", help="path to the prompt text file")
    p.add_argument("--min-shared", type=int, default=2, dest="min_shared",
                   help="minimum shared keywords for priority warnings (default 2)")
    p.add_argument("--json", dest="as_json", action="store_true",
                   help="emit findings as JSON")
    a = p.parse_args(argv)

    try:
        with open(a.prompt, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        print("error: cannot read prompt: %s" % e, file=sys.stderr)
        sys.exit(2)

    try:
        findings = analyze_rules(text, min_shared=a.min_shared)
    except TypeError as e:
        print("error: %s" % e, file=sys.stderr)
        sys.exit(2)

    s = summarize(findings)
    if a.as_json:
        print(json.dumps({"findings": findings, "summary": s},
                         ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print("[%s] %s %s: %s" % (f["severity"], f["location"],
                                      f["code"], f["message"]))
        print("summary: %d error(s), %d warn(s), %d info(s)"
              % (s["error"], s["warn"], s["info"]))
    sys.exit(1 if s["error"] else 0)


if __name__ == "__main__":
    main()
