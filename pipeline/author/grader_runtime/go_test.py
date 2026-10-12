"""Go-only private tests with completion evidence outside candidate control.

The compiler reads private suites as root. The resulting execute-only binary
runs as the ordinary grader user. Completion records are inserted into the
trusted declarations with Go's parser, and are bound to fresh, unguessable
tokens; candidate stdout and a zero process exit cannot fabricate them.
"""

INSTRUMENTER = r'''
package main
import (
 "bytes"
 "crypto/sha256"
 "encoding/hex"
 "encoding/json"
 "fmt"
 "go/ast"
 "go/format"
 "go/parser"
 "go/token"
 "os"
 "strings"
 "unicode"
)
type Request struct { Files map[string]string; Select map[string][]string; Secret string }
type Result struct { Files map[string]string; Cases []string; Tokens map[string]map[string]string }
func main() {
 var in Request
 if err:=json.NewDecoder(os.Stdin).Decode(&in); err!=nil { panic(err) }
 out:=Result{Files:map[string]string{}, Tokens:map[string]map[string]string{}}
 fset:=token.NewFileSet()
 seen:=map[string]bool{}
 for path, source:=range in.Files {
  f,err:=parser.ParseFile(fset,path,source,parser.ParseComments); if err!=nil { panic(err) }
  testingName:=""
  for _,imp:=range f.Imports { if imp.Path.Value==`"testing"` { testingName="testing"; if imp.Name!=nil { testingName=imp.Name.Name } } }
  selected:=in.Select[path]
  found:=map[string]bool{}
  for _,decl:=range f.Decls {
   fn,ok:=decl.(*ast.FuncDecl); if !ok || fn.Recv!=nil || fn.Body==nil { continue }
   name:=fn.Name.Name
   if !strings.HasPrefix(name,"Test") || name=="TestMain" || (len(name)>4 && unicode.IsLower([]rune(name[4:])[0])) { continue }
   if fn.Type.Params==nil || len(fn.Type.Params.List)!=1 || fn.Type.Results!=nil { continue }
   field:=fn.Type.Params.List[0]
   star,ok:=field.Type.(*ast.StarExpr); if !ok { continue }
   proper:=false
   if sel,ok:=star.X.(*ast.SelectorExpr); ok { if base,ok:=sel.X.(*ast.Ident); ok { proper=base.Name==testingName && sel.Sel.Name=="T" } }
   if ident,ok:=star.X.(*ast.Ident); ok { proper=testingName=="." && ident.Name=="T" }
   if !proper { continue }
   found[name]=true
   wanted:=len(selected)==0
   for _,s:=range selected { if s==name { wanted=true } }
   if !wanted { continue }
   if seen[name] { panic("duplicate private test: "+name) }; seen[name]=true
   if len(field.Names)==0 || (len(field.Names)==1 && field.Names[0].Name=="_") { field.Names=[]*ast.Ident{ast.NewIdent("arenaT")} }
   if len(field.Names)!=1 { panic("private test needs one testing.T parameter: "+name) }
   arg:=field.Names[0].Name
   tokens:=map[string]string{}
   for _,status:=range []string{"passed","failed","skipped"} {
    h:=sha256.Sum256([]byte(in.Secret+"\x00"+name+"\x00"+status)); tokens[status]="arena-go-completed:"+hex.EncodeToString(h[:])
   }
   statement:=fmt.Sprintf(`package p; func x(){ %s.Cleanup(func(){ if %s.Failed(){ arenagoFmt.Println(%q) } else if %s.Skipped(){ arenagoFmt.Println(%q) } else { arenagoFmt.Println(%q) } }) }`,arg,arg,tokens["failed"],arg,tokens["skipped"],tokens["passed"])
   snippet,err:=parser.ParseFile(token.NewFileSet(),"",statement,0); if err!=nil { panic(err) }
   stmt:=snippet.Decls[0].(*ast.FuncDecl).Body.List[0]
   // Register first: Go runs cleanup functions in reverse registration order,
   // so assertions in later cleanups contribute to the final status.
   fn.Body.List=append([]ast.Stmt{stmt},fn.Body.List...)
   out.Cases=append(out.Cases,name); out.Tokens[name]=tokens
  }
  for _,s:=range selected { if !found[s] { panic("selected private test not declared: "+path+"::"+s) } }
  used:=false; for _,name:=range out.Cases { if found[name] { used=true } }
  if used {
   imp:=&ast.ImportSpec{Name:ast.NewIdent("arenagoFmt"), Path:&ast.BasicLit{Kind:token.STRING,Value:`"fmt"`}}
   f.Decls=append([]ast.Decl{&ast.GenDecl{Tok:token.IMPORT,Specs:[]ast.Spec{imp}}},f.Decls...)
  }
  var buf bytes.Buffer; if err:=format.Node(&buf,fset,f); err!=nil { panic(err) }
  out.Files[path]=buf.String()
 }
 if len(out.Cases)==0 { panic("private selection contains no Go test declarations") }
 if err:=json.NewEncoder(os.Stdout).Encode(out); err!=nil { panic(err) }
}
'''

HELPER = r'''
_GO_PRIVATE_CACHE = None
_GO_INSTRUMENTER_BINARY = None

def _go_private_cache() -> Path:
    """Snapshot the image's warmed cache before any candidate code runs."""
    global _GO_PRIVATE_CACHE
    if _GO_PRIVATE_CACHE is None:
        import tempfile
        _GO_PRIVATE_CACHE = Path(tempfile.mkdtemp(prefix="arena-go-cache-"))
        warmed = Path("/opt/deps/gocache")
        if warmed.is_dir():
            shutil.copytree(warmed, _GO_PRIVATE_CACHE, dirs_exist_ok=True)
        _GO_PRIVATE_CACHE.chmod(0o700)
    return _GO_PRIVATE_CACHE

def _go_private_check(check: dict, tree: Path, toolchain: dict, manifest: dict | None) -> dict:
    import secrets
    import tempfile
    started = time.monotonic()
    timeout = float(check["runner"].get("timeout_s", 120))
    runner = check["runner"]
    result = {"passed": False, "exit_code": None, "stdout": "", "stderr": "", "seconds": 0.0}
    private = None
    processes = []
    try:
        if os.geteuid() != 0 or _grader_ids() is None:
            raise ValueError("private Go tests require the root verifier and its unprivileged grader user")
        reaching = introspection_problems(tree, manifest or {}, toolchain.get("vendored", []))
        if reaching:
            raise ValueError("candidate code reaches into the hidden test process: " + "; ".join(reaching[:4]))
        relative = PurePosixPath(runner.get("cwd", "."))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Go module cwd must be a relative path inside the working tree")
        module = (tree / str(relative)).resolve()
        if not module.is_relative_to(tree.resolve()) or not (module / "go.mod").is_file():
            raise ValueError("Go private runner cwd must contain the candidate go.mod")
        selection = runner.get("select") or []
        if not selection or len(set(selection)) != len(selection):
            raise ValueError("Go private tests need a unique non-empty suite selection")
        files, chosen = {}, {}
        for item in selection:
            rel, sep, name = item.partition("::")
            path = PurePosixPath(rel)
            if path.is_absolute() or ".." in path.parts or not rel.endswith("_test.go"):
                raise ValueError("Go selections must name private _test.go files")
            suite = (HERE / "suites" / rel).resolve()
            if not suite.is_relative_to((HERE / "suites").resolve()) or not suite.is_file():
                raise ValueError("selected private Go suite is missing: " + rel)
            files[rel] = suite.read_text()
            if sep:
                if not name or "::" in name:
                    raise ValueError("Go selection must be file_test.go or file_test.go::TestName")
                chosen.setdefault(rel, []).append(name)
            else:
                chosen[rel] = []
        # Private compiler inputs must stay outside the candidate module: Go's
        # embed patterns otherwise incorporate these files into application
        # assets during the root compilation, despite their file permissions.
        private = Path(tempfile.mkdtemp(prefix="arena_private_"))
        _chown_tree(tree, _grader_ids())
        os.chown(private, 0, 0)
        private.chmod(0o711)
        env = {"PATH": os.environ.get("PATH", "/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin"),
               "HOME": "/tmp", "LANG": "C.UTF-8", "TZ": "UTC", "GOPROXY": "off",
               "GOMODCACHE": "/opt/deps/gomod", "GOFLAGS": "-mod=mod", "GOWORK": "off",
               "GOCACHE": str(_go_private_cache())}
        # Toolchain data supplies ordinary build settings, not alternative
        # compiler programs or candidate-controlled command-line flags.
        for key in ("GOMODCACHE", "CGO_ENABLED", "TZ"):
            if key in toolchain.get("env", {}): env[key] = str(toolchain["env"][key])
        if "TZ" in runner.get("env", {}): env["TZ"] = str(runner["env"]["TZ"])
        def execute(argv, *, input_data=None, drop=False):
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0: raise subprocess.TimeoutExpired(argv, timeout)
            proc = subprocess.Popen(argv, cwd=str(module), env=env, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                    preexec_fn=_drop_privileges(_grader_ids()) if drop else None)
            processes.append(proc)
            try: out, err = proc.communicate(input=input_data, timeout=remaining)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL); proc.communicate(); raise
            return proc.returncode, out, err
        global _GO_INSTRUMENTER_BINARY
        if _GO_INSTRUMENTER_BINARY is None:
            helper = _go_private_cache() / "instrument.go"
            helper.write_text(GO_TEST_INSTRUMENTER)
            helper.chmod(0o600)
            binary = _go_private_cache() / "instrument"
            code, out, err = execute(["go", "build", "-o", str(binary), str(helper)])
            if code: raise ValueError("could not build trusted Go instrumenter: " + err.decode("utf-8", "replace")[-2000:])
            _GO_INSTRUMENTER_BINARY = binary
        payload = json.dumps({"Files": files, "Select": chosen, "Secret": secrets.token_hex(32)}).encode()
        code, out, err = execute([str(_GO_INSTRUMENTER_BINARY)], input_data=payload)
        if code: raise ValueError("invalid private Go selection: " + err.decode("utf-8", "replace")[-2000:])
        instrumented = json.loads(out)
        for index, source in enumerate(instrumented["Files"].values()):
            target = private / ("suite_%d_test.go" % index)
            target.write_text(source); target.chmod(0o600)
        binary = private / "suite.test"
        package = str(private)
        overlay_args = []
        if "package" in runner:
            relative_package = PurePosixPath(runner["package"])
            if relative_package.is_absolute() or ".." in relative_package.parts:
                raise ValueError("Go package must be relative to the module cwd")
            package_dir = (module / str(relative_package)).resolve()
            if not package_dir.is_relative_to(module) or not package_dir.is_dir():
                raise ValueError("Go package must exist inside the candidate module")
            named = runner.get("overlay") or {}
            if not isinstance(named, dict) or any(key not in files for key in named):
                raise ValueError("Go overlay keys must name selected private suites")
            mappings = {}
            for index, rel in enumerate(instrumented["Files"]):
                basename = named.get(rel, private.name + "_%d_test.go" % index)
                if not isinstance(basename, str) or PurePosixPath(basename).name != basename or not basename.endswith("_test.go"):
                    raise ValueError("Go overlay targets must be _test.go basenames inside the named package")
                target = str(package_dir / basename)
                if target in mappings: raise ValueError("Go overlay targets must be unique")
                mappings[target] = str(private / ("suite_%d_test.go" % index))
            overlay = private / "overlay.json"
            overlay.write_text(json.dumps({"Replace": mappings})); overlay.chmod(0o600)
            overlay_args = ["-overlay=" + str(overlay)]
            package = "./" + str(relative_package)
        elif runner.get("overlay"):
            raise ValueError("Go overlay requires a named package")
        else:
            code, out, err = execute(["go", "list", "-m", "-json"])
            if code: raise ValueError("could not identify the candidate Go module: " + err.decode("utf-8", "replace")[-2000:])
            metadata = json.loads(out)
            if metadata.get("Main") is not True or not isinstance(metadata.get("Path"), str):
                raise ValueError("Go private runner cwd must identify the candidate main module")
            code, out, err = execute(["go", "env", "GOVERSION"])
            version = re.fullmatch(r"go([0-9]+\.[0-9]+(?:\.[0-9]+)?)", out.decode().strip())
            if code or version is None:
                raise ValueError("Go private runner requires a pinned release compiler version")
            # Go module internal-import access follows the importer's logical
            # path. A separate checker module under that prefix can import the
            # candidate's internal packages without living in its directory.
            checker = private / "go.mod"
            checker.write_text("module " + metadata["Path"] + "/" + private.name + "\n\ngo " + version[1] + "\n")
            checker.chmod(0o600)
            workspace = private / "go.work"
            workspace.write_text("go " + version[1] + "\nuse (\n" + json.dumps(str(module)) + "\n" + json.dumps(str(private)) + "\n)\n")
            workspace.chmod(0o600)
            # Workspace mode also preserves candidate dependency replacements,
            # including modules whose import paths end with a major version.
            env["GOWORK"] = str(workspace)
            env["GOFLAGS"] = "-mod=readonly"
        code, out, err = execute(["go", "test", *overlay_args, "-c", "-vet=off", "-o", str(binary), package])
        if code:
            result.update(exit_code=code, stdout=_tail(out.decode("utf-8", "replace")), stderr=_tail(err.decode("utf-8", "replace")), error="could not compile the selected private Go tests")
            return result
        # Candidate packages cannot read either the instrumented test sources
        # or the binary that contains its fresh completion tokens.
        binary.chmod(0o711)
        env.pop("GOCACHE", None)
        env["GOWORK"] = "off"
        env["GOFLAGS"] = "-mod=mod"
        env["ARENA_REPO"] = str(tree)
        env["ARENA_HISTORY"] = os.environ.get("ARENA_HISTORY", "/arena-history")
        env["ARENA_STEP"] = os.environ.get("ARENA_STEP", "")
        expression = "^(?:" + "|".join(re.escape(name) for name in instrumented["Cases"]) + ")$"
        code, out, err = execute([str(binary), "-test.count=1", "-test.run=" + expression], drop=True)
        lines = out.decode("utf-8", "replace").splitlines()
        counts = {"passed": 0, "failed": 0, "skipped": 0}
        complete = True
        for name in instrumented["Cases"]:
            evidence = [(status, lines.count(value)) for status, value in instrumented["Tokens"][name].items()]
            seen = [(status, n) for status, n in evidence if n]
            if len(seen) != 1 or seen[0][1] != 1: complete = False
            else: counts[seen[0][0]] += 1
        # Remove private tokens from diagnostic output as well as the tree.
        redacted = [line for line in lines if not line.startswith("arena-go-completed:")]
        result.update(exit_code=code, stdout=_tail("\n".join(redacted)), stderr=_tail(err.decode("utf-8", "replace")),
                      tests=counts if complete else None,
                      passed=bool(complete and code==0 and counts["passed"] and not counts["failed"] and not counts["skipped"]))
        if not complete: result["error"] = "runner produced no trustworthy report of completed tests for every selected private declaration; exit status alone is not evidence"
        elif counts["failed"] or counts["skipped"]: result["error"] = "a failed or skipped private Go test is not a pass"
    except subprocess.TimeoutExpired:
        result.update(timed_out=True, error="timed out after %.0fs" % timeout)
    except (OSError, ValueError, KeyError) as exc:
        result["error"] = "grader: " + str(exc)
    finally:
        if private is not None:
            shutil.rmtree(private, ignore_errors=True)
        result["seconds"] = round(time.monotonic() - started, 3)
    return result

'''


def render(source: bytes) -> bytes:
    base = source.decode("utf-8")
    if "GO_TEST_INSTRUMENTER =" in base:
        raise ValueError("The runtime already contains a Go specialization")
    base = base.replace("\ndef run_command(", "\nGO_TEST_INSTRUMENTER = " + repr(INSTRUMENTER) + "\n\n" + HELPER + "\ndef run_command(", 1)
    needle = 'def run_command(check: dict, tree: Path, toolchain: dict, manifest: dict | None = None) -> dict:\n'
    assert needle in base
    base = base.replace(needle, needle + '    if check["runner"]["type"] == "go":\n        return {"passed": False, "exit_code": None, "stdout": "", "stderr": "", "seconds": 0.0, "error": "Legacy Go stdout reports are not trustworthy; use go-test with private suite selections"}\n    if check["runner"]["type"] == "go-test":\n        return _go_private_check(check, tree, toolchain, manifest)\n', 1)
    needle = '    result = grade(step, checks_doc, manifest, repo, work)\n'
    assert needle in base
    base = base.replace(needle, '    _go_private_cache()\n' + needle, 1)
    compile(base, "reviewed-go-grader.py", "exec")
    return base.encode("utf-8")
