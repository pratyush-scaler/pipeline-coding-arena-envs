"""Public output contracts. Stdlib-only; copied into every exported verifier.

This deliberately supports a documented subset of JSON Schema. Unknown schema
keywords fail at authoring time instead of silently weakening validation.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path, PurePosixPath


class ContractError(ValueError):
    pass


def contained(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative or "\0" in relative:
        raise ContractError("expected a nonempty POSIX relative path")
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ContractError(f"path leaves its root: {relative!r}")
    root = Path(root).resolve()
    target = root.joinpath(*rel.parts)
    for part in (target, *target.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ContractError(f"symlink is not an allowed artifact: {relative!r}")
    if not target.resolve().is_relative_to(root):
        raise ContractError(f"path leaves its root: {relative!r}")
    return target


def strict_json(text: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ContractError(f"nonfinite JSON value: {value}")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, TypeError) as exc:
        raise ContractError(f"invalid JSON: {exc}") from exc


KEYWORDS = {"type", "properties", "required", "additionalProperties", "items",
            "minItems", "maxItems", "uniqueItems", "enum", "description",
            "minimum", "maximum", "minLength", "maxLength", "pattern", "unordered"}
TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def nullable_scalar(kind) -> str | None:
    """The supported union is one scalar type plus null, in either order."""
    if (not isinstance(kind, list) or len(kind) != 2
            or any(not isinstance(value, str) for value in kind)
            or kind.count('null') != 1):
        return None
    other = next(value for value in kind if value != 'null')
    return other if other in {'string', 'integer', 'number', 'boolean'} else None


def allows_null(schema: dict) -> bool:
    return schema.get('type') == 'null' or nullable_scalar(schema.get('type')) is not None


def finite_number(value) -> bool:
    return type(value) is int or type(value) is float and math.isfinite(value)


def schema_problems(schema: dict, where: str = "schema") -> list[str]:
    if not isinstance(schema, dict):
        return [f"{where}: expected a schema object"]
    errors = [f"{where}: unsupported keywords {sorted(set(schema) - KEYWORDS)}"] if set(schema) - KEYWORDS else []
    kind = schema.get("type")
    if isinstance(kind, list):
        base = nullable_scalar(kind)
        if base is None:
            return errors + [f"{where}: a type union must contain null and exactly one scalar type"]
        return schema_problems({**schema, 'type': base}, where)
    if not isinstance(kind, str) or kind not in TYPES:
        return errors + [f"{where}: type must be one of {sorted(TYPES)}"]
    if kind == "object":
        props, required = schema.get("properties"), schema.get("required")
        if not isinstance(props, dict) or not isinstance(required, list):
            return errors + [f"{where}: objects need properties and required"]
        if schema.get("additionalProperties") is not False:
            errors.append(f"{where}: additionalProperties must be false")
        if any(not isinstance(x, str) or x not in props for x in required):
            errors.append(f"{where}: required contains an undeclared field")
        for key, sub in props.items():
            if not isinstance(key, str) or not re.fullmatch(r"[^.\[\]]+", key):
                errors.append(f"{where}: field names cannot contain dots or brackets")
            errors += schema_problems(sub, f"{where}.{key}")
    if kind == "array":
        errors += schema_problems(schema.get("items"), f"{where}[]")
    allowed = {"object": {"properties", "required", "additionalProperties"},
               "array": {"items", "minItems", "maxItems", "uniqueItems", "unordered"},
               "string": {"minLength", "maxLength", "pattern"},
               "number": {"minimum", "maximum"}, "integer": {"minimum", "maximum"}}
    wrong = set(schema) - {"type", "enum", "description"} - allowed.get(kind, set())
    if wrong:
        errors.append(f"{where}: keywords do not apply to {kind}: {sorted(wrong)}")
    for flag in ("uniqueItems", "unordered"):
        if flag in schema and type(schema[flag]) is not bool:
            errors.append(f"{where}.{flag}: expected boolean")
    for lo, hi in (("minimum", "maximum"), ("minItems", "maxItems"), ("minLength", "maxLength")):
        if type(schema.get(lo)) in (int, float) and type(schema.get(hi)) in (int, float) and schema[lo] > schema[hi]:
            errors.append(f"{where}: {lo} exceeds {hi}")
    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            errors.append(f"{where}.{key}: expected nonnegative integer")
    for key in ("minimum", "maximum"):
        if key in schema and not finite_number(schema[key]):
            errors.append(f"{where}.{key}: expected finite number")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        errors.append(f"{where}.enum: expected nonempty array")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (re.error, TypeError):
            errors.append(f"{where}.pattern: invalid regular expression")
    return errors


def schema_errors(value, schema: dict, where: str = "$") -> list[str]:
    kind = schema["type"]
    if isinstance(kind, list):
        base = nullable_scalar(kind)
        if base is None:
            raise ContractError('unsupported type union in schema')
        if value is None:
            if 'enum' in schema and None not in schema['enum']:
                return [f'{where}: value is outside the published enum']
            return []
        return schema_errors(value, {**schema, 'type': base}, where)
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str),
             # JSON Schema: 3.0 is an integer; a bool is not.
             "integer": type(value) is int or (type(value) is float and value.is_integer()),
             "number": finite_number(value),
             "boolean": type(value) is bool, "null": value is None}[kind]
    if not valid:
        return [f"{where}: expected {kind}, got {type(value).__name__}"]
    errors = []
    if "enum" in schema and not any(type(value) is type(v) and value == v for v in schema["enum"]):
        errors.append(f"{where}: value is outside the published enum")
    if kind == "object":
        props = schema["properties"]
        errors += [f"{where}.{k}: required field missing" for k in schema["required"] if k not in value]
        errors += [f"{where}.{k}: undeclared field" for k in value if k not in props]
        for key in value.keys() & props.keys():
            errors += schema_errors(value[key], props[key], f"{where}.{key}")
    if kind == "array":
        for i, item in enumerate(value):
            errors += schema_errors(item, schema["items"], f"{where}[{i}]")
        if schema.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in value}) != len(value):
            errors.append(f"{where}: repeated array entries")
    if kind in ("array", "string"):
        low, high = ("minItems", "maxItems") if kind == "array" else ("minLength", "maxLength")
        if len(value) < schema.get(low, 0) or len(value) > schema.get(high, math.inf):
            errors.append(f"{where}: length outside the published bounds")
    if kind == "string" and "pattern" in schema and not re.search(schema["pattern"], value):
        errors.append(f"{where}: does not match the published pattern")
    if kind in ("number", "integer") and not schema.get("minimum", -math.inf) <= value <= schema.get("maximum", math.inf):
        errors.append(f"{where}: number outside the published bounds")
    return errors


def schema_at(schema: dict, path: str) -> dict | None:
    cur = schema
    for token in path.split(".") if path else []:
        array = token.endswith("[]")
        key = token[:-2] if array else token
        if cur.get("type") != "object" or key not in cur.get("properties", {}):
            return None
        cur = cur["properties"][key]
        if array:
            if cur.get("type") != "array":
                return None
            cur = cur["items"]
    return cur


def submission_errors(root: Path, outputs: list[dict], step: str | None = None,
                      allow_missing: bool = False) -> list[str]:
    errors = []
    # Declared artifacts from other steps may coexist (including retained
    # earlier submissions). An unrelated site, report or hidden file is not an
    # authorized deliverable merely because the requested JSON is also present.
    allowed = {output['file'] for output in outputs}
    parents = {parent.as_posix() for name in allowed
               for parent in Path(name).parents if parent.as_posix() != '.'}
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink() or (path.is_dir() and rel not in parents) or (path.is_file() and rel not in allowed):
            errors.append(f"{rel}: undeclared deliverable or symlink")
    for output in outputs:
        if step is not None and output["step"] != step:
            continue
        try:
            path = contained(root, output["file"])
            if allow_missing and not path.exists():
                continue
            doc = strict_json(path.read_text())
            errors += schema_errors(doc, output["schema"], output["file"])
        except (OSError, UnicodeError, ContractError) as exc:
            errors.append(f"{output['file']}: {exc}")
    return errors


def compile_steps(spec: dict) -> list[dict]:
    """Both designers and the builder use this exact step/check assignment."""
    from copy import deepcopy
    if spec.get("instruction"):
        steps = [{"name": "01-task", "instruction": spec["instruction"],
                  "deliverable_file": spec.get("deliverable_file")}]
        second = spec.get("second_step")
        if second:
            if not isinstance(second, dict) or not second.get("instruction"):
                raise ContractError("second_step needs an instruction")
            steps.append({"name": "02-revision", **deepcopy(second)})
    else:
        steps = deepcopy(spec.get("steps") or [])
    if not steps or any(not isinstance(s, dict) or not isinstance(s.get("name"), str)
                        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", s["name"])
                        or not isinstance(s.get("instruction"), str) or not s["instruction"].strip() for s in steps):
        raise ContractError("missing or invalid step names")
    if len({s["name"] for s in steps}) != len(steps):
        raise ContractError("duplicate step names")
    names = {s["name"] for s in steps}
    step_forms = ("use a step name (" + ", ".join(repr(s["name"]) for s in steps)
                  + f") or its 1-based number (1..{len(steps)})")
    by_file = {s["deliverable_file"]: s["name"] for s in steps if s.get("deliverable_file")}
    files = set()
    for output in spec.get("outputs") or []:
        if not isinstance(output, dict) or not isinstance(output.get("file"), str):
            raise ContractError("each output needs a filename")
        if output["file"] in files:
            raise ContractError("each output file must have exactly one owning step; use a new file for revisions")
        files.add(output["file"])
        step = output.get("step")
        if type(step) is int and 1 <= step <= len(steps):
            output["step"] = step = steps[step - 1]["name"]
        if step not in names:
            raise ContractError(f"output {output.get('file')}: unknown step {step!r}; {step_forms}")
        if output["file"] in by_file and by_file[output["file"]] != step:
            raise ContractError(f"output {output['file']}: contradicts deliverable_file step")
        by_file[output["file"]] = step
    for check in spec.get("checks", []):
        for key in ("id", "dimension"):
            value = check.get(key, "core")
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
                raise ContractError(f"invalid check {key}: {value!r}")
        # A check takes the same step forms as an output. It used to accept
        # only the name, so `step: 2` -- what the prompt's "assign it to step
        # 2" reads as -- failed with no hint of the valid forms, and `0` passed
        # as unassigned and silently fell into step 1. Twelve of 40 designs in
        # build 2026-09-26 hit it; a-046 spent its last 18 steps on it.
        assigned = check.get("step")
        if type(assigned) is int and 1 <= assigned <= len(steps):
            assigned = steps[assigned - 1]["name"]
        inferred = by_file.get(check.get("actual", {}).get("file"))
        if assigned is not None and assigned not in names:
            raise ContractError(f"check {check['id']}: unknown step {assigned!r}; {step_forms}, "
                                "or omit it to follow the check's output file")
        if assigned and inferred and assigned != inferred:
            raise ContractError(f"check {check['id']}: step contradicts output ownership")
        check["step"] = assigned or inferred or steps[0]["name"]
    by_id = {c["id"]: c for c in spec.get("checks", [])}
    if len(by_id) != len(spec.get("checks", [])):
        raise ContractError("duplicate check ids")
    visiting, done = set(), set()
    def visit(cid):
        if cid in visiting:
            raise ContractError(f"cyclic check dependencies at {cid}")
        if cid in done:
            return
        visiting.add(cid)
        c = by_id[cid]
        for dep in c.get("requires", []):
            if dep not in by_id or by_id[dep]["step"] != c["step"]:
                raise ContractError(f"{cid}: dependency {dep} is missing or belongs to another step")
            visit(dep)
        visiting.remove(cid)
        done.add(cid)
    for cid in by_id:
        visit(cid)
    spec["steps"] = steps
    return steps


#: Where the agent reads and writes. Stated as absolute paths in every step's
#: contract: a bare file name gives the agent no way to know which directory it
#: is relative to, and an agent that guesses writes somewhere the grader never
#: looks. Kept in step with `task.toml` artifacts and the agent Dockerfile.
INPUT_ROOT = "/app/repo"
OUTPUT_ROOT = "/app/deliverables"


def public_contract(outputs: list[dict], step: str) -> str:
    selected = [o for o in outputs if o["step"] == step]
    if not selected:
        return ""
    files = ", ".join(f"{OUTPUT_ROOT}/{o['file']}" for o in selected)
    return ("\n\nOutput contract\n\n"
            f"Inputs: every supplied source file is in {INPUT_ROOT} (read-only). A bare "
            f"file name in this assignment that is not a deliverable is relative to {INPUT_ROOT}. "
            "Leave every supplied source file byte-for-byte unchanged.\n\n"
            f"Outputs: write each deliverable as UTF-8 JSON, without Markdown fences, to "
            f"{OUTPUT_ROOT}/<file>. This step writes: {files}. These schemas define the "
            "required fields and types. "
            "Produce only files declared by this task; declared artifacts from other steps may coexist. "
            "Undeclared files, unrelated directories and symlinks earn zero for this step. "
            "Duplicate keys and nonfinite numbers are invalid. An invalid or missing required file "
            "earns zero for this step. Array order is compared as written, except for an array whose "
            'schema says "unordered": true or that the assignment says may be in any order.\n\n'
            "Equality and set comparisons ignore letter case, repeated whitespace, smart quotes/dashes, "
            "leading Markdown heading/list/quote markers and trailing heading hashes. Numeric "
            "strings ignore thousands separators. These comparison rules do not relax schema constraints.\n\n"
            + "\n\n".join(f"{OUTPUT_ROOT}/{o['file']}:\n```json\n{json.dumps(o['schema'], indent=2, ensure_ascii=False)}\n```" for o in selected))
