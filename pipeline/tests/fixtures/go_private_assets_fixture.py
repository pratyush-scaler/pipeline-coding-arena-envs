"""Check that private test staging leaves normal application assets intact."""
import json, tempfile
from pathlib import Path
from types import ModuleType

grade=ModuleType('private_go_asset_fixture');grade.__file__='/source/grade.py'
exec(compile(Path(grade.__file__).read_bytes(),grade.__file__,'exec'),grade.__dict__)
top=Path(tempfile.mkdtemp(prefix='go-asset-fixture-'));top.chmod(0o755)
private=top/'tests';(private/'suites').mkdir(parents=True);grade.HERE=private
(private/'suites/proof_test.go').write_text('''package arena_private
import("testing";"example.com/proof/internal/backend")
func TestPublicAssetCount(t *testing.T){if n:=backend.AssetCount();n!=2{t.Fatalf("application asset count changed: got %d, want 2",n)}}
''')
tree=top/'repo';(tree/'internal/backend').mkdir(parents=True)
(tree/'go.mod').write_text('module example.com/proof\ngo 1.23\n')
(tree/'assets.go').write_text('''package proof
import("embed";"io/fs")
//go:embed all:internal
var assets embed.FS
func AssetCount()int{n:=0;fs.WalkDir(assets,"internal",func(path string,d fs.DirEntry,e error)error{if e==nil&&!d.IsDir(){n++};return e});return n}
''')
(tree/'internal/backend/backend.go').write_text('package backend\nimport "example.com/proof"\nfunc AssetCount()int{return proof.AssetCount()}\n')
(tree/'internal/backend/public.txt').write_text('ordinary public asset')
result=grade.run_command({'runner':{'type':'go-test','select':['proof_test.go'],'timeout_s':180}},tree,{'env':{'GOMODCACHE':'/opt/deps/gomod'},'vendored':[]},{})
print(json.dumps(result),flush=True)
Path('/out/asset-staging-result.json').write_text(json.dumps({'schema':'arena-coding/go-private-staging-assets/1','expected_public_asset_count':2,'result':result},indent=2)+'\n')
