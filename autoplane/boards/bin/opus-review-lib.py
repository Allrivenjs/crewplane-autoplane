"""Utilidades comunes de opus-validate y opus-revalidate."""
import os, re, subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = os.environ.get("CORTEX_PLAN", "tencent-agent-memory")
D = ROOT / "docs/plans" / PLAN
R = ROOT / "crewplane-multi-agent-sandbox/.agent/revisiones" / ("" if PLAN == "tencent-agent-memory" else PLAN)
RUNS = ROOT / ".crewplane/opus-runs" / PLAN
for p in (R, R / "v3", R / "correcciones", R / "v1", RUNS):
    p.mkdir(parents=True, exist_ok=True)
os.environ["CORTEX_PLAN"] = PLAN
tw = SourceFileLoader("tw", str(ROOT / ".crewplane/bin/task-workflow")).load_module()
TOOLS = ["--allowedTools", "Read", "Grep", "Glob", "Bash(git show:*)", "Bash(git log:*)", "Bash(git diff:*)"]
MODEL = os.environ.get("OPUS_MODEL", "claude-opus-5-5")


def rows(mod):
    out = []
    for l in (D / "SEGUIMIENTO.md").read_text().splitlines():
        m = re.match(rf"^\| *\[({mod}-\d\d)\]", l)
        if m:
            c = [x.strip() for x in l.split("|")]
            sha = re.search(r"`([0-9a-f]{7,})`", c[5])
            out.append((m.group(1), c[2], c[3], sha.group(1) if sha else None))
    return out


def module_plan(task_id):
    return (D / tw.find_task(task_id)[0]).read_text()


def ask(prompt, tag):
    (RUNS / f"{tag}.prompt").write_text(prompt)
    r = subprocess.run(["claude", "--model", MODEL, "-p", *TOOLS], input=prompt, text=True,
                       capture_output=True, cwd=ROOT, timeout=3600)
    out = r.stdout.strip()
    (RUNS / f"{tag}.out").write_text(out + "\n\nSTDERR:\n" + r.stderr[-1500:])
    if r.returncode != 0 or len(out) < 200:
        raise SystemExit(f"{tag}: FALLO {r.returncode} {out[:200]}")
    return out


def extract_corrections(text):
    n = 0
    for m in re.finditer(r"^### Corrección ([A-Z]\d\d-\d\d)\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S):
        (R / "correcciones" / f"{m.group(1)}.md").write_text(m.group(2).strip() + "\n")
        n += 1
    return n


def verdicts(text):
    return re.findall(r"^## ([A-Z]\d\d-\d\d) — (\w+)", text, re.M)
