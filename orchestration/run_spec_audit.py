"""Run spec_audit.py on each task's home workspace; collect results into
deliverable-zips-coding-v2/spec-audit.json (+ SPEC-AUDIT.md rendered by render_audit)."""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.benchmark import coder

V1 = V2 = str(Path(__file__).resolve().parents[1])
KZ = set()
SRC = Path(V1, "pipeline-artifacts/waves/spec_audit.py").read_text()
ADJ = {"c-002-express-glb-viewer": ["package__iNq8Nuj", "iNq8Nuj"], "c-003-firebase-web-app": ["package__3NMREsq", "3NMREsq"],
       "c-019-icon-asset-generator": ["package__P9niPZb", "P9niPZb"]}
tasks = json.loads(Path(sys.argv[1]).read_text())       # [{ws, bundle, name, fable_campaign}]
out_path = Path(sys.argv[2])
results = json.loads(out_path.read_text()) if out_path.exists() else {}
by = {}
for t in tasks:
    t["adjudicated"] = ADJ.get(t["name"], [])
    by.setdefault(t["ws"], []).append(t)
for ws, ts in by.items():
    root = V1 if ws in KZ else V2
    payload = json.dumps(ts)
    cmd = (f"cat > /tmp/spec_audit.py <<'PYX'\n{SRC}\nPYX\ncat > /tmp/spec_audit_tasks.json <<'JSX'\n{payload}\nJSX\n"
           f"S=$(ls -td {root}/.staged/*/ | head -1); cd $S && PYTHONPATH=$S {root}/.venv/bin/python /tmp/spec_audit.py /tmp/spec_audit_tasks.json 2>&1 | grep '^AUDIT'")
    t0 = time.time()
    text = coder._clean(coder.ssh(ws, cmd, timeout=3600, check=False).stdout.decode(errors="replace"))
    for line in text.splitlines():
        if line.startswith("AUDIT "):
            r = json.loads(line[6:]); r["workspace"] = ws; r["audited_at"] = time.strftime("%Y-%m-%d %H:%M")
            results[r["name"]] = r
    print(f"{ws}: {len(ts)} task(s) in {time.time()-t0:.0f}s", flush=True)
    out_path.write_text(json.dumps(results, indent=1))
print("audited", len(results))
