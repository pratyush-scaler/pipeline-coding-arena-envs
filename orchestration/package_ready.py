"""Audit + package + zip the ready coding tasks into deliverable-zips-coding-v2/ (user, 2026-10-05).

1. spec audit on each task's workspace (spec_audit.py) -> spec-audit.json, SPEC-AUDIT.md
2. package on each workspace (package_remote.py): deliver + runs/ + REWARD-HACKING.md + privacy rescan
3. pull the delivered tree and zip each task in Arena's format as <env-id>@<version>.zip (one folder
   <env-id>/ inside; pipeline/arena_format.py), a multi-step task as one package
4. INDEX.md / index.json

usage: package_ready.py <tasks.json>     tasks: [{ws, bundle, name, fable_campaign}]
"""
import json, shutil, subprocess, sys, time, zipfile, hashlib
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.benchmark import coder, coder_run

#: Zips land in ship-zips/ of this checkout.
ROOT = Path(__file__).resolve().parents[1]
V1 = V2 = ROOT
KZ = set()
OUT = ROOT / "ship-zips"
DATE = time.strftime("%Y%m%d")
WAVES = ROOT / "orchestration"   # the packaging scripts live beside this one
#: Tasks whose independent-oracle authoring files may be redacted in the delivered copy
#: (user, 2026-10-05 ~23:00 IST: "Then lets redact and zip these tasks").
REDACT_ORACLE: set = set()
#: Unscannable files closed by visual review (sha256 -> what was seen), 2026-10-05.
REVIEWED = {
    "1bdd6123c697b350d7080f9b9ae26bb5800330deae1ca2482da0889d6cc09ad7":
        "review 2026-10-06: c-019-icon-asset-generator run artifact ic_weather.png -- a generated app icon; valid PNG with no text/metadata chunks; the scanner's email pattern matched 6 bytes inside compressed IDAT pixel data (binary noise); viewed: no personal or identifying data",
    "f2432042728c45a879d5733eb90b3789a788d624554e143b8b3bf062e9cfa660":
        "review 2026-10-06: c-019-icon-asset-generator run artifact ic_browser.png -- a generated app icon; valid PNG with no text/metadata chunks; the scanner's email pattern matched 7 bytes inside compressed IDAT pixel data (binary noise); viewed: no personal or identifying data",
    "61e9fce2747a1baefa24c322c975a55d3722fef9cbbc7b8e1f7c28a45a41bd4d":
        "review 2026-10-06: c-019-icon-asset-generator run artifact ic_clock.png -- a generated app icon; valid PNG with no text/metadata chunks; the scanner's email pattern matched 7 bytes inside compressed IDAT pixel data (binary noise); viewed: no personal or identifying data",
    "76dbe59ca949c1e43c05c73a05bc24788761ee5a25f4fe274033b5554780f84a":
        "review 2026-10-06: c-019-icon-asset-generator run artifact ic_settings.png -- a generated app icon; valid PNG with no text/metadata chunks; the scanner's email pattern matched 6 bytes inside compressed IDAT pixel data (binary noise); viewed: no personal or identifying data",
    "66a7393727fd016a0228b8200a82c32e2df5d2e1e5faf28f28b7b47d463b0f91":
        "review 2026-10-06: c-019-icon-asset-generator run artifact ic_browser.png -- a generated app icon; valid PNG with no text/metadata chunks; the scanner's email pattern matched 10 bytes inside compressed IDAT pixel data (binary noise); viewed: no personal or identifying data",
    "fb197828b0bde2e6a0ac9f514213c2d2262211b046d84f5b4bfebd7bef1d6fda":
        "review 2026-10-06: c-019-icon-asset-generator run artifact neon-all-icons.png -- a generated app icon sheet labelled only with icon names; valid PNG with no text/metadata chunks; the scanner's email pattern matched 6 bytes inside compressed IDAT pixel data (binary noise); viewed: no personal or identifying data",
    "d1d2b8fa30c374bec7450b4b9a412f778e7cf104ec5f85b949083a202f89aff6":
        "review 2026-10-06: c-019-icon-asset-generator run artifact ic_shield.png -- a generated app icon; valid PNG with no text/metadata chunks; the scanner's email pattern matched 7 bytes inside compressed IDAT pixel data (binary noise); viewed: no personal or identifying data",
    "4038add34f6c9fc15f3d9fb6d9399b06a7e9d992b8a521f8c3070304c6feb9e1":
        "review 2026-10-06: c-019-icon-asset-generator run artifact ic_calendar.png -- a generated app icon; valid PNG with no text/metadata chunks; the scanner's email pattern matched 6 bytes inside compressed IDAT pixel data (binary noise); viewed: no personal or identifying data",
    "3066e2b1b97d7035fbe8f1cc85914819a8a69405e39df310699f629d45b1214d":
        "visual review 2026-10-05: c-005-webgl Astra run screenshot neon-city-webgl.png -- the agent's WebGL game render "
        "(HUD: HP/Cash/Wanted, minimap, Romanian hint text, touch controls); no personal or identifying data",
    "36b63ca6ce8c628cada17e016a8b5e1f3d137b6dea5fb7bdea6f7f14bfef89a5":
        "visual review 2026-10-05: c-005-webgl Astra run screenshot neon-city-fixed.png -- the agent's WebGL city render "
        "(HUD, minimap, cars, street); no personal or identifying data",
    "0e7dea73c76132072d23757ed2f1d5e049d8ec9fff440aa865974ca45404a13c":
        "content review 2026-10-05: c-011-anki Astra run artifact deck.apkg -- unzipped (collection.anki2 + empty media map); "
        "its 60 notes (German verb+preposition cards, Persian meanings, example sentences: the task's own data) and deck "
        "model scanned with pipeline.privacy.scan_text: no findings",
}


def _cover_row(dest):
    """Cover-sheet failure-cause columns from the delivered package's verification/failure-causes.json."""
    from pipeline import failure_report
    doc = None
    try:
        doc = json.loads((Path(dest) / failure_report.RECEIPT).read_text())
    except (OSError, ValueError):
        return None
    return failure_report.cover_row(doc)


def write_cover(index: dict) -> None:
    """ship-zips/COVER.md: one row per shipped task with the failure-cause columns."""
    lines = ["# Cover sheet (ship15)", "",
             "| task | steps | Fable pass@1 (mean) | Astra pass@1 (mean) | decisive trap type(s) | independent hard decisions |"
             " free weight | why the models failed |", "|---|---:|---|---|---|---:|---:|---|"]
    for name, t in sorted((index.get("tasks") or {}).items()):
        fc = t.get("failure_causes") or {}
        f, a = t.get("fable_10") or {}, t.get("astra_10") or t.get("astra_5") or {}
        why = "<br>".join(str(x).replace("|", "\\|") for x in (fc.get("why_models_failed") or []))
        lines.append(f"| {name} | {len(t.get('steps') or [])} | {f.get('pass_at_1')} ({f.get('mean_reward')}) | "
                     f"{a.get('pass_at_1')} ({a.get('mean_reward')}) | {', '.join(fc.get('decisive_trap_types') or [])} | "
                     f"{fc.get('independent_hard_decisions')} | {fc.get('free_weight_share')} | {why} |")
    (OUT / "COVER.md").write_text("\n".join(lines) + "\n")


def sh(ws, cmd, timeout=3600):
    return coder._clean(coder.ssh(ws, cmd, timeout=timeout, check=False).stdout.decode(errors="replace"))


def main():
    tasks = json.loads(Path(sys.argv[1]).read_text())
    OUT.mkdir(exist_ok=True)
    audit_path = OUT / "spec-audit.json"
    subprocess.run([str(V2 / ".venv/bin/python"), str(WAVES / "run_spec_audit.py"), sys.argv[1], str(audit_path)], check=True, cwd=V2)
    audit = json.loads(audit_path.read_text())
    index_path = OUT / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {"tasks": {}}
    index["tasks"] = {k: v for k, v in index.get("tasks", {}).items() if (OUT / v.get("zip", "-")).is_file()}
    names = {t["name"] for t in tasks}
    index["held"] = {k: v for k, v in (index.get("held") or {}).items() if k not in names}
    src = (WAVES / "package_remote.py").read_text()
    by = {}
    for t in tasks:
        a = audit.get(t["name"]) or {}
        camp = a.get("campaigns") or {}
        by.setdefault(t["ws"], []).append({**t, "astra_campaigns": camp.get("astra") or [], "oracle_campaign": camp.get("oracle"),
                                           "reviewed_unscannable": REVIEWED,
                                           "redact_oracle": t["name"] in REDACT_ORACLE,
                                           "audit": {"flags": a.get("flags"), "scanned": a.get("scanned"), "fixed": a.get("fixed")}})
    stamp = time.strftime("%H%M%S")
    for ws, ts in by.items():
        root = V1 if ws in KZ else V2
        rdir = f"/tmp/pkg-{stamp}"
        cmd = (f"rm -rf {rdir}; mkdir -p {rdir}; cat > {rdir}/package_remote.py <<'PYX'\n{src}\nPYX\n"
               f"cat > {rdir}/tasks.json <<'JSX'\n{json.dumps(ts)}\nJSX\n"
               f"S=$(ls -td {root}/.staged/*/ | head -1); cd $S && PYTHONPATH=$S {root}/.venv/bin/python {rdir}/package_remote.py {rdir}/tasks.json {rdir}/o 2>&1 | tail -5")
        text = sh(ws, cmd)
        line = [l for l in text.splitlines() if l.startswith("PKG ")]
        if not line:
            print(f"{ws}: packaging produced no report: {text[-800:]}")
            continue
        local = Path(f"/tmp/pkg-{stamp}-{ws}")
        if local.exists():
            shutil.rmtree(local)
        local.mkdir(parents=True)
        # Compressed, bounded, and resumed once (--partial): the package directory is mostly compressible data and
        # the pull used to run uncompressed for up to an hour while the driver waited (2026-10-18).
        for attempt in range(2):
            try:
                coder_run.rsync(ws, f"{rdir}/o/", f"{local}/", push=False, timeout=1800, compress=True)
                break
            except (subprocess.TimeoutExpired, RuntimeError) as exc:
                print(f"{ws}: package pull attempt {attempt + 1} failed: {type(exc).__name__}: {str(exc)[-200:]}")
                if attempt:
                    raise
        rep = json.loads((local / "package-report.json").read_text())
        for h in rep["held"]:
            index["held"][h["name"]] = {"why": h["why"], "level": h.get("level"), "at": time.strftime("%Y-%m-%d %H:%M")}
            print(f"{h['name']}: HELD -- {h['why'][:240]}")
        for d in rep["delivered"]:
            owner = next((k for k, v in index["tasks"].items() if v.get("short_name") == d["short_name"] and k != d["name"]), None)
            if owner:
                # two tasks under one package name overwrite each other's zip (c-003 / c-010 canvas, 2026-10-08)
                why = f"package name {d['short_name']} is already delivered for {owner}; package both together so each gets a distinct name"
                index["held"][d["name"]] = {"why": why, "level": d.get("level"), "at": time.strftime("%Y-%m-%d %H:%M")}
                print(f"{d['name']}: HELD -- {why}")
                continue
            out_root = local / "out"
            dest = out_root / d["destination"]
            # A task delivered before is replaced: its package carries the next minor version, recorded in the
            # delivery receipt (task bank CONTRIBUTING step 2; 2026-10-21, the audit repairs).
            if d["name"] in index["tasks"]:
                from pipeline.deliver import bump_version
                d["version"] = bump_version(dest)
            # Arena's format (Scaler x Arena weekly, 2026-10-22, "Formatting Changes - P0"): one
            # <env-id>@<version>.zip in Arena's layout. The reproduction run (pipeline.arena_repro) attaches
            # verification/receipt.json and the oracle evidence; a package the exporter refuses is held.
            from pipeline import arena_format
            try:
                zname = arena_format.package(dest, OUT, work=local).name
            except Exception as exc:  # noqa: BLE001 -- hold the task with the reason, package the rest
                why = f"Arena format export failed: {type(exc).__name__}: {exc}"[:600]
                index["held"][d["name"]] = {"why": why, "level": d.get("level"), "at": time.strftime("%Y-%m-%d %H:%M")}
                print(f"{d['name']}: HELD -- {why}")
                continue
            for old in [*OUT.glob(f"{d['short_name']}_*.zip"), *OUT.glob(f"{d['short_name']}@*.zip")]:
                if old.name != zname:
                    old.unlink()
            zips = [zname]
            # A multi-turn task ships as ONE package (Spec Appendix A: the turns share one environment and
            # TURNS.md); per-step zips kept a task.toml declaring every step and Harbor refused them.
            if False:
                for i, step in enumerate(d["steps"], 1):
                    sname = f"{d['short_name']}_step-{i:02d}_{DATE}.zip"
                    with zipfile.ZipFile(OUT / sname, "w", zipfile.ZIP_DEFLATED) as z:
                        for f in sorted(dest.rglob("*")):
                            rel = f.relative_to(dest).parts
                            if len(rel) >= 2 and rel[0] == "steps" and rel[1] != step:
                                continue
                            z.write(f, f.relative_to(out_root).as_posix())
                    zips.append(sname)
            a = audit.get(d["name"]) or {}
            cal = a.get("calibration") or {}
            index["tasks"][d["name"]] = {
                "zip": zname, "step_zips": zips[1:], "short_name": d["short_name"], "level": d["level"], "steps": d["steps"],
                "version": (d.get("version") or {}).get("assigned"),
                "destination": d["destination"], "workspace": ws, "source": next(t["bundle"] for t in ts if t["name"] == d["name"]),
                "fable_10": {k: (cal.get("fable") or {}).get(k) for k in ("pass", "scored", "pass_at_1", "pass_at_4", "mean_reward")},
                "astra_5": {k: (cal.get("astra") or {}).get(k) for k in ("pass", "scored", "pass_at_1", "pass_at_4", "mean_reward")},
                "astra_10": ({k: (cal.get("astra") or {}).get(k) for k in ("pass", "scored", "pass_at_1", "pass_at_4", "mean_reward")}
                             if ((cal.get("astra") or {}).get("scored") or 0) >= 10 else None),
                "audit_fails": [k for k, c in (a.get("checks") or {}).items() if c["status"] != "pass"],
                # Arena (Oct 12): cover-sheet columns explaining why the models failed.
                "failure_causes": _cover_row(dest),
                "accepted_release_receipt": d.get("accepted"), "acceptance_problems": d.get("acceptance_problems"),
                "zip_sha256": hashlib.sha256((OUT / zname).read_bytes()).hexdigest(), "zipped_at": time.strftime("%Y-%m-%d %H:%M")}
            index["held"].pop(d["name"], None)
            print(f"{d['name']}: {', '.join(zips)} ({d['level']})")
    index["schema"] = "arena/ship15-zips/1"
    index["updated"] = time.strftime("%Y-%m-%d %H:%M")
    index_path.write_text(json.dumps(index, indent=1))
    write_cover(index)
    subprocess.run([sys.executable, str(WAVES / "render_audit.py")], check=True)
    print(f"{len(index['tasks'])} task(s) zipped, {len(index['held'])} held -> {OUT}")


if __name__ == "__main__":
    main()
