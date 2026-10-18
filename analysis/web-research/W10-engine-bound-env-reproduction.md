# W10: Recovering engine-bound seeds without faking the environment (web research, 2026-10-12)

The question. Selection kills engine-bound seeds as `no_runtime` (`pipeline/select/score.py` K_RUNTIME,
`pipeline/config.py` `ENGINE_LANGUAGES`, `pipeline/author/toolchain.py` `NO_RUNTIME`). Which of them can
be rebuilt as Harbor tasks that:

- run on the customer runtime (Linux amd64, Harbor 0.20.0, self-hosted E2B Firecracker, 2 vCPU / 4 GiB,
  no GPU, no nested KVM, no display unless we start Xvfb, no network at run time);
- build reproducibly later from pinned or vendored artifacts;
- are graded offline by hidden tests that exercise **real** behaviour, with no homemade simulator;
- use only licences that allow use (and ideally redistribution) inside an evaluation image?

Companion files: W1 to W6 (benchmarks, failure modes, hardening). This file is about toolchains only.

---

## §0 Method, evidence tags, and what is actually killed

**Method.** About 30 web searches and 35 direct page fetches (official docs, READMEs, licence pages). I
also queried live registries:

- the GitHub API via `gh` (licence, archive status, latest release, asset names and sizes);
- the Debian archive through `api.ftp-master.debian.org/madison` for bookworm (now `oldstable`) and
  trixie (now `stable`);
- PyPI JSON, Maven Central and repo.jenkins-ci.org metadata, the Docker Hub tag API, the Flutter
  release index, and the .NET release index.

I downloaded two release zips into `/tmp/w10dl` and inspected them: the Luau CLI and Lune. I listed their
contents and the highest glibc/libstdc++ symbol versions they need. I ran nothing from them.

**Tags.**

- **VERIFIED** means read in a primary source or registry response during this session (URL given).
- **INFERRED** means my reasoning or general knowledge, not re-checked here.
- Sizes are compressed download or registry sizes unless stated.

**What is killed.** Source: `pipeline-artifacts/index.json` (kaizen dump, 500 sessions, 2045 seeds), local
and VERIFIED. 223 seed rows are `engine`, covering **42 unique sessions**. Grouped by dominant language and
what the code really is (file names from `src_files`):

| dominant language | sessions | what they are |
|---|---:|---|
| lua | 16 | Roblox game code (6): 019f9775 baseball game in Rojo-style folders, 019fced8 bus-above-ground, 019e9980 hitbox system, 019e6b16 viewport, 019e6715 ProximityPrompt merge, 01a00187 first-person camera. **Roblox exploit or cheat scripts (5)**: 019eed98 "auto farm" LocalScript, 019ea704 `flex hub.lua`, 019e9403 `severe_admin_panel.luau`, 019ff6be a deobfuscated "combat farm", plus 019e5bc7 `XHit_NeverLose.lua` (a CS:GO cheat-menu script). Garry's Mod (2): 019ea725 (inventory, shop, quests; 301 files), 019ea7d5 (TFA-style module system). Factorio mod (1): 019ffb24 `space-map-rework`. GoreBox mod via PlayFab (1): 019f1241. WoW private-server addon (1): 01a01bf4 |
| java | 7 | Minecraft: 019e7c76 Paper plugin, 019e8486 Velocity plus a Spigot plugin (`pom.xml`), 019eb0a9 three Gradle mods or plugins, 019eb9e5 Fabric 1.21 mods, 019ef532 client renderer (MCP/Mojang code). Android Java: 019f79e3, 019f880c |
| gdscript | 5 | 019e72b8, 019f03bd, 019f19ed, 019f6f99 (asks for **Godot 4.4.1**), 019ff0b5 |
| mql5 | 3 | 019e9823 indicator and panel, 019ebac4 prop-firm EA with a 3 % daily-drawdown rule, 019fae87 trend EA |
| csharp | 3 | 019ecaa0 Unity store simulation and placement system, 019fbda1 Unity "FigmaFlow" runtime (state machine, transition builder), 019f4e99 a Visual Studio 2015 desktop C# project |
| kotlin | 2 | Android apps 019e8b35 and 019f73e8 |
| objc | 2 | **Misclassified MATLAB** (`.m` maps to `objc` in `config.py:404`): 019ea7b5, 019f6ef1 |
| dart | 2 | Flutter: 019fa93f VoIP/SIP clean-architecture app (3.8 MB of Dart), 019f52b1 store app |
| r | 1 | 019f80c3 bioinformatics R scripts. R falls through to `engine` because it is in no language set (`config.py:474`) |
| groovy | 1 | 019f92f5 Jenkins declarative pipeline (42 files) |

`analysis/Rejected-traces_B1.csv` records 31 of these as "Needs special platform". It records 019ea725
as also "Judged by looks only", so a runtime alone will not save every seed.

The earlier 370-session corpus (`analysis/CODING-TRACES.md` §3) also carried FPGA HDL and more Unity,
Jenkins and MQL5. None of the HDL seeds is in the current index.

Two local findings matter as much as the web research:

- **Not every kill is about the engine.** `java` is in `ENGINE_LANGUAGES` and there is no JVM toolchain in
  `TOOLCHAINS` (python, node, browser, go, rust, php, c, shell). R and MATLAB are killed by the
  language-map fallthrough. Adding a JVM toolchain and an R/Octave toolchain recovers runtime for 10
  sessions with no engine question at all.
- **Five Lua sessions are game cheats or exploits.** Whatever the runtime, they should be excluded on
  policy grounds before anyone builds a runtime for them.

---

## §1 Cross-cutting doctrine: a fidelity ladder

The reviewer rejected a homemade Vue runtime or fake DOM several times. The safe line is: **run the
real engine, or the engine's own official harness, or the real language toolchain on code that genuinely
does not need the engine. Never author a stand-in for an engine API and grade against it.**

| level | what runs | examples | what a hidden test may assert |
|---|---|---|---|
| L4 real engine, headless | the actual engine or framework | Godot `--headless` with GUT or gdUnit4; `flutter test`; Robolectric (real AOSP framework jars); Icarus, Verilator or GHDL under cocotb; Jenkins core | anything observable: scene tree, signals, physics ticks, widget trees, Activity lifecycle, RTL waveforms |
| L3 engine data model, no simulation | the engine's official file format, reflection DB and datatypes, without a scheduler, physics or network | Lune `@lune/roblox` over a place built by Rojo | instance hierarchy, attributes and tags, datatype maths (CFrame, Vector3), pure ModuleScript logic |
| L2 real language toolchain on engine-free code | the real compiler or interpreter | .NET SDK on Unity code with no `UnityEngine` reference; MockBukkit (a community harness for the Bukkit API); JenkinsPipelineUnit (the community harness for Jenkinsfiles) | the behaviour of extracted pure logic |
| L1 static only | parsers, linters, type checkers | `luau-analyze`, `luau-lsp analyze`, `glualint`, MetaEditor `/s` | **guardrail only, never an objective**: "still type-checks", "still parses" |
| L0 port | the logic re-expressed in another language | MQL5 EA ported to Python | an inspired task (F1/F0): different language, declared as such |

Rule of thumb for task design on L2 and L3: scope the graded clause to the logic layer the trace's user
actually corrected, and say so in the instruction in the user's voice. Engine-facing code that cannot run
is held only by an L1 guardrail.

Using a framework's **official** test double is not a fake simulator: MockBukkit, JenkinsPipelineUnit,
Flutter's `TestDefaultBinaryMessenger` and Robolectric shadows are the ecosystem's own standard test
substrates. A per-task stub of `RunService` or `DataStoreService` is a fake simulator. INFERRED: this is
my reading of the reviewer's past rejections, and the semantic review should confirm it on the first
task of each new toolchain.

---

## §2 Ecosystems

### 2.1 Roblox / Luau

**Tool inventory (VERIFIED via `gh api`, 2026-10-12).**

| tool | version / date | licence | Linux amd64 artifact | notes |
|---|---|---|---|---|
| Luau CLI ([luau-lang/luau](https://github.com/luau-lang/luau/releases)) | 0.742, 2026-10-10 | MIT | `luau-ubuntu.zip` (7 MB): `luau`, `luau-analyze`, `luau-ast`, `luau-compile` (21 MB unzipped) | needs GLIBC_2.34 and GLIBCXX_3.4.30 (I inspected the binaries), so it runs on bookworm (glibc 2.36, gcc-12 libstdc++) |
| Lune ([lune-org/lune](https://github.com/lune-org/lune)) | v0.11.0, 2026-10-11 (v0.10.5 earlier) | MPL-2.0 | `lune-0.11.0-linux-x86_64.zip`: one 13.8 MB binary, GLIBC_2.34 | standalone Luau runtime with `@lune/roblox`, `@lune/task`, `@lune/luau`, fs, process |
| Rojo ([rojo-rbx/rojo](https://github.com/rojo-rbx/rojo)) | v7.7.1, 2026-10-02 | MPL-2.0 | `rojo-7.7.1-linux-x86_64.zip` (5 MB) | CLI subcommands `build`, `sourcemap`, `syncback`, `serve`, `upload` ([src/cli](https://github.com/rojo-rbx/rojo/tree/master/src/cli)). `build --output` writes `.rbxl`, `.rbxlx`, `.rbxm` or `.rbxmx`. `syncback` "exists to convert Roblox files into a Rojo project automatically" |
| luau-lsp ([JohnnyMorganz/luau-lsp](https://github.com/JohnnyMorganz/luau-lsp)) | 1.70.1, 2026-09-27 | MIT | `luau-lsp-linux-x86_64.zip` | `luau-lsp analyze` is a standalone CI mode "with full Rojo resolution and API types support" |
| selene, darklua, wally | 0.32.0 / v0.19.0 / v0.4.0-alpha | MPL-2.0 / MIT / MPL-2.0 | Linux zips | linter (Roblox std), bundler, package manager |
| TestEZ ([Roblox/TestEZ](https://github.com/Roblox/TestEZ)) | v0.4.2 (2022) | Apache-2.0 | n/a | **archived** (2024). Its README names Lemur for CI |
| Lemur ([LPGhatguy/lemur](https://github.com/LPGhatguy/lemur)) | 2017 | MIT | n/a | **archived** (2020). A homemade Roblox emulator, which is exactly the kind of fake the reviewer rejects |
| Jest-Lua ([jsdotlua/jest-lua](https://github.com/jsdotlua/jest-lua)) | v3.10.0 (2024-12) | MIT | n/a | README: "can currently only run inside of Roblox". Lune support is an open help-wanted issue |
| run-in-roblox | v0.3.0 (2020) | MIT | n/a | drives Roblox Studio (Windows/macOS only), so **unusable** |
| roblox-ts | v3.0.0 | MIT | npm | TS-to-Luau compiler. Its output still needs a Roblox instance tree at run time |
| Open Cloud Luau Execution ([docs](https://create.roblox.com/docs/en-us/cloud/reference/features/luau-execution.md)) | n/a | Roblox ToS | n/a | runs Luau in a real place server via `apis.roblox.com` with an `x-api-key`, tasks up to 5 min. Needs network and credentials, so **unusable offline** |

**How far real game logic runs headlessly (Lune).** VERIFIED from the
[API status page](https://lune-org.github.io/docs/roblox/4-api-status) and the
[roblox API reference](https://lune-org.github.io/docs/api-reference/roblox).

What works:

- `deserializePlace` and `deserializeModel` read real `.rbxl`/`.rbxm` files, and serialize writes them.
- About 23 `Instance` methods: `new`, `Clone`, `Destroy`, `FindFirstChild*`, `FindFirstAncestor*`,
  `GetChildren`, `GetDescendants`, attributes, CollectionService-style tags, `IsA`, `GetFullName`.
- `DataModel:GetService` and `FindService`.
- 23 Roblox datatypes with real implementations, including CFrame, Vector3, Color3, UDim2 and Region3.
- `getReflectionDatabase` exposes the bundled reflection database.
- `implementMethod` and `implementProperty` let you add behaviour to a class.
- `luau.load(source, {environment, injectGlobals, debugName})` loads a chunk in a custom environment
  ([docs](https://lune-org.github.io/docs/api-reference/luau)).
- `@lune/task` provides spawn, defer, delay, wait and cancel
  ([docs](https://lune-org.github.io/docs/api-reference/task)).

What does not work (INFERRED from the absence of these in the API status page):

- no instance events (`Changed`, `ChildAdded`, `Touched`) and no signals;
- no physics, no `RunService` stepping, no `Players.LocalPlayer` or `PlayerAdded`, no replication;
- no `DataStoreService` methods and no `workspace` spatial queries (`GetPartBoundsInBox`, `Raycast`).

`implementMethod` could fill these gaps, but doing so would be authoring a fake engine, which is
precisely what the reviewer rejects.

One more gap: darklua's `convert_require` cannot read Roblox-style requires; `roblox` is supported only as
a target ([docs](https://darklua.com/docs/rules/convert_require/)), VERIFIED. So `require(script.Parent.X)`
needs a small loader. That loader walks the deserialized tree and `luau.load`s each ModuleScript's
`Source` with `script` and `game` bound. It is glue for the module system, not an engine API. INFERRED:
no published standard loader exists. I searched and found none.

**What people do in Linux CI (VERIFIED in part).**

- Static checks: selene, StyLua, `luau-lsp analyze` with Roblox definitions.
- Unit tests of pure modules under Lune. An example repo,
  [dinoderek/rob](https://github.com/dinoderek/rob), runs `lune run test` with no credentials.
- Engine tests via Open Cloud (for example, Jest Roblox's CI path). No one runs Studio on Linux.

| | |
|---|---|
| Feasible strategy | Build the fixture as a Rojo project, using the trace's tree; Rojo `syncback` covers seeds that only had place files. At grade time: `rojo build -o game.rbxl`, then a Lune harness that `deserializePlace`s it, loads ModuleScripts through a small require-glue, and asserts on pure logic, datatype maths, instance hierarchy, attributes and tags. `luau-analyze` / `luau-lsp analyze --definitions` (Roblox `globalTypes.d.luau`, vendored) acts as a guardrail. The harness emits TAP or JUnit for `grade.py` |
| Fidelity | **L3**. Real Luau semantics, real Roblox datatypes and file format, real hierarchy API. Nothing event-driven, physical, networked or persistent. For the current seeds: 019f9775 config, scoring and level modules are good; 019fced8 bus placement is CFrame/size maths, testable if expressed as a function; 019e9980 hitbox geometry maths is testable, spatial queries are not; 019e6b16 viewport, 019e6715 prompt events and 01a00187 camera are mostly engine behaviour, so poor |
| Image size | +~70 MB over the base (luau 21 MB, lune 14 MB, rojo, luau-lsp, selene). VERIFIED sizes |
| Offline recipe | Download the release zips by exact tag, check sha256, unzip to `/opt/rbx/bin`; or vendor them in `environment/deps/` (MIT and MPL allow it; for MPL, link or ship the tagged source tarball). Vendor `globalTypes.d.luau` and the API dump from the luau-lsp tag. No network is needed at run time. Set `login_shell_path` |
| Licensing | MIT / MPL-2.0 / Apache-2.0, all redistributable. Roblox's engine and Studio are not involved |
| E2B risks (2 vCPU / 4 GiB) | Negligible: single static binaries, millisecond runs |
| Verdict | **Recover with effort** for logic-layer seeds (019f9775 first, then 019fced8 and 019e9980). Whole-game behaviour: **not feasible**. Exploit scripts: **exclude** |

**Garry's Mod (GLua).**

- [GLuaTest](https://github.com/CFC-Servers/GLuaTest) (GPL-3.0, active) runs tests inside a real srcds.
  Its [Dockerfile](https://github.com/CFC-Servers/GLuaTest/tree/main/docker) does
  `steamcmd +login anonymous +app_update 4020 -beta $GMOD_BRANCH validate`, run twice to work around
  "Missing configuration". It then clones requirements and workshop content from the network. VERIFIED.
- The Facepunch wiki says a GSLT token is required for a listed server
  ([wiki](https://wiki.facepunch.com/gmod/Downloading_a_Dedicated_Server)). VERIFIED. INFERRED: `sv_lan 1`
  avoids it.
- Disk is roughly 5 to 8 GB (hosting guides, INFERRED).
- No redistribution terms were found for the dedicated-server files. INFERRED: Valve's Steam Subscriber
  Agreement does not grant redistribution.
- SteamCMD `app_update` installs whatever the branch currently is, so versions cannot be pinned. Pinning a
  depot manifest needs `download_depot`, and INFERRED: that may need a logged-in account.
- GLua adds C-style syntax (`!=`, `&&`, `||`, `//`, `continue`), so vanilla LuaJIT 2.1 cannot even parse
  much of it. [GLuaFixer/glualint](https://github.com/FPtje/GLuaFixer) (LGPL-2.1, 1.30.0) parses and lints
  GLua: L1 only.

| | |
|---|---|
| Feasible strategy | (a) srcds plus GLuaTest: real engine, but unpinnable, not redistributable, 5 to 8 GB, and needs Steam at build time. (b) Extract plain-Lua modules (no GLua syntax, no engine globals) and test them under LuaJIT 2.1 or Lua 5.1 with busted (MIT, Debian `lua-busted`): L2 on very little code |
| Fidelity | (a) L4. (b) L2 on a small fraction; GMod addons are mostly hooks, net messages and VGUI |
| Image size | (a) +5 to 8 GB. (b) +5 MB |
| Licensing | (a) Valve proprietary, redistribution unknown, so assume no. (b) MIT |
| E2B risks | (a) srcds was historically 32-bit. An x86-64 branch exists, but whether the guest kernel has IA32 emulation is unverified. Memory is fine |
| Verdict | **Not feasible** under the reproducibility and licence constraints. (b) only as an F1 extraction when a seed's corrected logic is plain Lua; 019ea725 and 019ea7d5 do not look like that from file names (`cl_`/`sv_` hooks) |

### 2.2 Unity C#

- **Editor in Docker.** GameCI's `unityci/editor` images exist and are current (Unity 6000.3.26f1; ubuntu
  `linux-il2cpp` tag is 5.4 GB compressed, VERIFIED via the Docker Hub API). The base OS is Ubuntu or
  Windows only ([docs](https://game.ci/docs/docker/docker-images)). Every run must activate a licence:
  - Personal: a `.ulf` from Unity Hub plus `UNITY_EMAIL` and `UNITY_PASSWORD`.
  - Pro: `UNITY_SERIAL`.
  - Or a floating-licence server ([docs](https://game.ci/docs/github/activation)).
  VERIFIED.
- **Personal licences cannot be activated from the command line.** Unity's 6.3 LTS and 6.5 manuals say
  the manual or command-line method "doesn't work with floating license subscriptions or Unity Personal".
  Personal is activated by logging in to Unity Hub
  ([6.3 manual](https://docs.unity3d.com/Manual/ManualActivationCmdWin.html),
  [6.5 manual](https://docs.unity.cn/6000.5/Documentation/Manual/ManagingYourUnityLicense.html)). VERIFIED.
  Community reports confirm that manual Personal activation was withdrawn
  ([forum](https://discussions.unity.com/t/unity-no-longer-supports-manual-activation-of-personal-licenses/926760),
  [workaround hack](https://dev.to/ankursheel/workaround-for-unity-personal-license-manual-activation-not-supported-49j2)).
- **Unity's C# source cannot be used to build stand-ins.** UnityCsReference is under the
  [Unity Reference-Only License](https://unity.com/legal/licenses/unity-reference-only-license): "no other
  distribution or modification of the Software is permitted". VERIFIED.
- **NuGet "UnityEngine" packages are not a clean option.** Some point at a local install. The author of
  the main one calls bundling Unity's binaries of "questionable legality"
  ([libraries.io: Unity3D](https://libraries.io/nuget/Unity3D)). VERIFIED. There is no
  legally redistributable official stub of `UnityEngine.dll`. INFERRED: a homemade shim of
  `Vector3`/`Mathf`/`MonoBehaviour` is a fake runtime in the reviewer's sense.
- **The alternative.** The .NET SDK (10.0.401 LTS, 8.0.425 LTS; MIT;
  [release index](https://builds.dotnet.microsoft.com/dotnet/release-metadata/releases-index.json)) plus
  NUnit 4.6.1 or 5.0.0 (VERIFIED versions). Compile the subset of `Assets/**/*.cs` that has no `using
  UnityEngine` into a `netstandard2.1` or `net10.0` class library with `<LangVersion>9</LangVersion>`
  (Unity's C# level), and test that. Mono 6.12 (Debian trixie) also works. INFERRED.
- **What fraction is testable.** INFERRED from Unity community practice (the humble-object pattern; EditMode
  versus PlayMode tests): in hobbyist projects, typically 10 to 30 % of files are engine-free. Simulation
  or economy cores, state machines and grid maths are the usual candidates. Seeds 019ecaa0
  (`*StoreSimulation.cs`, `*PlacementSystem.cs`) and 019fbda1 (`FigmaStateMachine.cs`,
  `TransitionBuilder.cs`) have file names that suggest such cores. They must be inspected; Unity samples
  often reference `Vector2Int` or `Debug.Log` everywhere, which blocks compilation.

| | |
|---|---|
| Feasible strategy | Editor: none. Logic: .NET 10 SDK plus NUnit on engine-free classes, keeping the trace's file layout. Any `UnityEngine` reference disqualifies a file from the objective set |
| Fidelity | L2 on the engine-free subset. Zero on MonoBehaviour behaviour |
| Image size | Editor: 5.4 GB compressed (about 12 to 15 GB unpacked, INFERRED). .NET SDK path: about 200 MB tarball, about 700 MB to 1 GB installed with a warm NuGet cache (INFERRED) |
| Offline recipe | Fetch `dotnet-sdk-10.0.401-linux-x64.tar.gz` by exact URL and sha512 (from the release JSON), or vendor it (MIT). Run `dotnet restore` at build time into `NUGET_PACKAGES=/opt/deps/nuget`, then `dotnet test --no-restore`; or use a local feed with `--source /opt/deps/nuget-feed`. Set `DOTNET_CLI_TELEMETRY_OPTOUT=1` and `DOTNET_NOLOGO=1`. JUnit via the `JunitXml.TestLogger` package or the TRX logger (INFERRED) |
| Licensing | Editor: proprietary, per-seat activation, online. .NET and NUnit: MIT |
| E2B risks | Editor: activation needs the network, the image is above E2B's 10 GiB Hobby template disk. .NET: a cold `dotnet test` is about 10 to 30 s at 2 vCPU (INFERRED) |
| Verdict | Editor: **not feasible**. Pure-logic extraction: **recover with effort**, seed by seed, only where the corrected logic is engine-free |

### 2.3 Godot

- **Engine.** [Godot](https://github.com/godotengine/godot/releases) (MIT) 4.7.2-stable, 2026-08-18;
  `Godot_v4.7.2-stable_linux.x86_64.zip` is 74 MB. 3.6.3 also ships `linux_headless`/`server` builds.
  VERIFIED. Debian packages only Godot 3 (`godot` 3.6 in trixie), so use the official binary.
- **Headless flags.** `--headless` is "`--display-driver headless --audio-driver Dummy`. Useful for servers
  and with `--script`". `--import` "starts the editor, waits for any resources to be imported, and then
  quits". `--check-only` parses only. `--quit-after N` runs N iterations
  ([command-line docs](https://docs.godotengine.org/en/stable/tutorials/editor/command_line_tutorial.html)).
  VERIFIED.
- **GUT** (MIT). The compatibility table maps Godot 4.7 to GUT 9.7.1, 4.6 to 9.6.1, 4.5 to 9.5.0, 4.3/4.4
  to 9.4.0, and 3.x to 7.4.x. Run with `godot -d -s --path "$PWD" addons/gut/gut_cmdln.gd -gdir=...
  -gexit -gjunit_xml_file=...`; exit 0 means pass and 1 means fail
  ([GUT CLI](https://gut.readthedocs.io/en/latest/Command-Line.html), [repo](https://github.com/bitwes/Gut)).
  VERIFIED.
- **gdUnit4** (MIT). v6.2.2, 2026-10-07. v5.x covers 4.3 to 4.4.1; v6.0 to 6.2 cover 4.5 to 4.7.1. Run
  with `addons/gdUnit4/runtest.sh -a <dir>`, which writes JUnit `results.xml`; exit codes are 0, 100
  (failures) and 101 (warnings). It also supports C# tests
  ([CLI docs](https://godot-gdunit-labs.github.io/gdUnit4/latest/advanced_testing/cmd/),
  [repo](https://github.com/godot-gdunit-labs/gdUnit4)). VERIFIED.
- `barichello/godot-ci` (MIT) is 2.46 GB compressed because it bundles export templates. Tests do not need
  them. VERIFIED.

| | |
|---|---|
| Feasible strategy | Pin the Godot binary to the version the trace used (019f6f99 names 4.4.1, so pair it with GUT 9.4.0 or gdUnit4 v5.x). At image build: `godot --headless --import --path /app/repo` to populate `.godot/` (class_name cache, imported resources). Hidden tests are GUT/gdUnit4 suites in `tests/suites/`, copied into a fresh tree at grade time with the addon vendored. Tests may instance scenes, run frames, emit signals and step physics |
| Fidelity | **L4**: the real engine, scene tree, signals, timers and physics with a dummy renderer. Pixel output is not testable (dummy renderer); visual-only complaints stay out of scope |
| Image size | +~150 to 200 MB (binary about 74 MB zipped, INFERRED unpacked size) plus the addons |
| Offline recipe | `curl -fsSLO https://github.com/godotengine/godot/releases/download/4.4.1-stable/Godot_v4.4.1-stable_linux.x86_64.zip` with sha256 check (Godot publishes `SHA512-SUMS.txt`, INFERRED), or vendor the zip (MIT). Vendor GUT or gdUnit4 at a tag. Re-run `--import` in `test.sh` on the grading copy, because the agent may add resources |
| Licensing | MIT throughout |
| E2B risks | Low. Headless needs no X or GPU. INFERRED: import of a small project takes 5 to 30 s at 2 vCPU, memory 200 to 500 MB. Watch for the GUT CI freeze after an import step reported in [Gut#491](https://github.com/bitwes/Gut/issues/491). Always pass `-gexit` and an outer timeout |
| Verdict | **Recover now**. 5 seeds |

### 2.4 Flutter / Dart

- **Flutter SDK.** Stable 3.47.7 (2026-10-08, Dart 3.13.5). `flutter_linux_3.47.7-stable.tar.xz` is
  **1.58 GB** compressed ([release index](https://storage.googleapis.com/flutter_infra_release/releases/releases_linux.json)).
  VERIFIED. Linux prerequisites are `curl git unzip xz-utils zip libglu1-mesa`
  ([install docs](https://docs.flutter.dev/install/manual)). VERIFIED.
- **Pure Dart.** The `dart:stable` official image is 301 MB compressed. VERIFIED.
- **Community images.** `ghcr.io/cirruslabs/flutter` (MIT repo, active) exists.
  `cirrusci/flutter` on Docker Hub is stale (2023). VERIFIED.
- **Widget tests need no device.** "A widget test's environment is replaced with an implementation much
  simpler than a full-blown UI system". Integration tests need a device or emulator
  ([testing overview](https://docs.flutter.dev/testing/overview)). VERIFIED. `flutter test` runs headless
  in `flutter_tester`. A missing `libGLU.so.1` makes it crash with exit 127 (seen in an old image), hence
  `libglu1-mesa`.
- **Offline pub.** `pub get --offline` "looks only in your local package cache"; the cache location is set
  by `PUB_CACHE` ([pub get docs](https://dart.dev/tools/pub/cmd/pub-get)). VERIFIED.

| | |
|---|---|
| Feasible strategy | Pin the Flutter version from the trace's `pubspec.lock` or SDK constraint. At build: extract the SDK to `/opt/flutter`, then `flutter config --no-analytics`, `flutter precache --no-android --no-ios --no-web` (INFERRED flags), `flutter pub get` on the fixture with `PUB_CACHE=/opt/deps/pub-cache`. Warm-run `flutter test` once on the fixture. At grade: `flutter pub get --offline && flutter test --no-pub --reporter json` (JSON reporter for `grade.py`; INFERRED flags). Plugins that use platform channels (SIP, audio) are faked through Flutter's own `TestDefaultBinaryMessengerBinding` mocks, the official route |
| Fidelity | **L4** for widgets, state management (BLoC/Provider/Riverpod) and pure Dart logic. Platform plugins: only through official mocks. Pixel goldens are possible but font-sensitive |
| Image size | SDK about 3 to 4 GB unpacked (INFERRED from 1.58 GB xz). Prune `bin/cache/artifacts/engine/android-*` and `ios-*` and drop `.git` history for roughly 1.5 to 2.5 GB, plus the pub cache (100 to 500 MB). Pure Dart: about 300 MB |
| Offline recipe | Fetch the tarball by exact URL and check the release-index `sha256` (INFERRED field), or vendor it (BSD-3; size is the issue). Keep `git` installed: the flutter tool shells out to git for its version. Add `git config --system --add safe.directory /opt/flutter` (INFERRED) |
| Licensing | BSD-3 (Flutter and Dart), MIT (cirruslabs images). Redistributable |
| E2B risks | Image size versus the 10 to 20 GiB template disk (§3.4). A cold `flutter test` compiles the test to a kernel: 20 to 90 s at 2 vCPU, 1 to 1.5 GB RAM (INFERRED). `grade.py` copies the tree per check, so each check pays the compile; group checks or keep the grader's per-check cost in mind |
| Verdict | **Recover now** (with image-size care). 2 seeds; 019fa93f (244 files) is the richer one |

### 2.5 Android / Kotlin (and JVM in general)

- **Robolectric.** Latest 4.17. It fetches `android-all-instrumented` jars from Maven at run time unless
  `robolectric.offline=true` and `robolectric.dependency.dir=<dir>`
  ([configuring](https://robolectric.org/configuring/)). VERIFIED. `android-all-instrumented`
  17-robolectric-15733970-i7 is **228 MB** per API level and **Apache-2.0** (Maven Central POM). VERIFIED.
- **Android SDK licence.** §3.4: "you may not copy (except for backup purposes), modify, adapt,
  redistribute ... the SDK". §3.1: the licence is "solely to develop applications for compatible
  implementations of Android". §3.5: open-source components follow their own licences
  ([terms](https://developer.android.com/studio/terms)). VERIFIED. So **the SDK cannot be vendored into the
  package**; it must be downloaded at image build (dl.google.com, or Debian non-free
  `google-android-*-installer`, which itself downloads from Google). VERIFIED: the Debian packages exist
  in trixie non-free.
- **Gradle offline.** `--offline` uses only the cache and fails on any missing module. An incubating
  read-only cache, `GRADLE_RO_DEP_CACHE`, points at a directory holding `modules-2`, seeded by copying a
  real build's cache ([dependency caching](https://docs.gradle.org/current/userguide/dependency_caching.html)).
  VERIFIED.
- **Versions.** Gradle 9.8.1 (2026-10-07). AGP 9.4.1 on Google Maven. VERIFIED.
- **No emulator.** Firecracker exposes no KVM, so the Android emulator is out (INFERRED, standard).
- **Kotlin.** Debian's `kotlin` package is 1.3.31 (useless); Kotlin comes through Gradle. VERIFIED version.

| | |
|---|---|
| Feasible strategy | Plain JVM unit tests (`src/test`) for logic, plus Robolectric for Activity, Fragment, ViewModel, Room or Compose-on-JVM behaviour. At build: install cmdline-tools, run `sdkmanager "platforms;android-NN" "build-tools;X"` with licence acceptance, run `./gradlew testDebugUnitTest --no-daemon` once on the fixture to warm `GRADLE_USER_HOME=/opt/deps/gradle`, and pre-fetch the android-all jar for the target SDK into `/opt/deps/robolectric`. At grade: `./gradlew --offline testDebugUnitTest -Probolectric.offline=true ...`; JUnit XML is written to `build/test-results` |
| Fidelity | **L4-ish**: Robolectric runs the real AOSP framework jars with shadows for native parts; it is Google's recommended local-test path. No GPU, no real device behaviour |
| Image size | JDK 17/21 (~300 MB) + SDK platform and build-tools (~300 to 500 MB) + Gradle distribution and dependency cache with AGP and Kotlin (~1 to 1.5 GB) + android-all (228 MB per API) = **about 2.5 to 3.5 GB** (INFERRED) |
| Offline recipe | Build time needs network to dl.google.com, maven.google.com and Maven Central (or a pre-seeded `GRADLE_RO_DEP_CACHE` vendored as a tarball; open-source jars only, never SDK binaries). Pin AGP, Gradle and Kotlin from the trace's wrapper and build files; enable Gradle dependency verification (`verification-metadata.xml`, INFERRED) |
| Licensing | SDK: proprietary, no redistribution, so it must be fetched by whoever builds the image (legal review needed for an "evaluation" use under §3.1). Robolectric, AGP, Gradle, Kotlin, android-all: Apache-2.0 |
| E2B risks | **Memory**: Gradle daemon + Kotlin daemon + test JVM in 4 GiB is tight. Set `org.gradle.jvmargs=-Xmx1536m`, `kotlin.compiler.execution.strategy=in-process`, `maxParallelForks=1`, test `maxHeapSize=1g` (INFERRED). **Time**: AGP configuration plus compile plus Robolectric is about 1 to 4 min cold per invocation at 2 vCPU (INFERRED), and `grade.py` runs one runner per check. **Disk**: fine |
| Verdict | **Recover with effort** (4 seeds: 019e8b35, 019f73e8, 019f79e3, 019f880c). It needs a JVM toolchain first (§5 P0) |

**Minecraft Java (the 5 Java sessions).**

- **Paper/Bukkit plugins.** [MockBukkit](https://github.com/MockBukkit/MockBukkit) (MIT, v4.117.0,
  active) is the community's standard JVM harness: `org.mockbukkit.mockbukkit:mockbukkit-v1.21` with
  JUnit 5 and `paper-api` from repo.papermc.io
  ([getting started](https://docs.mockbukkit.org/docs/en/user_guide/introduction/getting_started)).
  VERIFIED. Caveat: `paper-api` is a `-SNAPSHOT`. Pin a timestamped snapshot or vendor the jar (GPL-3.0
  API, INFERRED).
- **Velocity plugins.** Use the Velocity API jar with JUnit (INFERRED).
- **Fabric, Forge and client mods (019eb9e5, 019ef532).** These need Mojang's game jars, which Loom
  downloads from Mojang; Mojang's EULA forbids redistribution. INFERRED: **not feasible** except for
  pure-logic extraction.
- **Verdict.** Paper/Spigot/Velocity plugins (019e7c76, 019e8486, possibly 019eb0a9): **recover with
  effort** once a JVM toolchain exists. Mods: **not feasible**.

### 2.6 MQL5 / MetaTrader 5

- **Compiler.** `metaeditor64.exe /compile:"<file>" [/include:"<MQL5 dir>"] [/log] [/s]`. The log is
  `<source>.log` beside the source; exit codes are undocumented
  ([MetaEditor docs](https://www.metatrader5.com/en/metaeditor/help/beginning/integration_ide)). VERIFIED.
  A third-party guide reports that Wine returns exit code 1 even on success, so check for the `.ex5` and
  the log ([skill](https://smithery.ai/skills/tmaru-eng/mql5-x-compile)). Single source, INFERRED-weak.
- **Docker prior art.**
  - [gmag11/MetaTrader5-Docker-Image](https://github.com/gmag11/MetaTrader5-Docker-Image) (MIT wrapper):
    Wine plus KasmVNC. It installs and auto-updates MT5 itself (about 600 MB, or about 4 GB with Python).
  - [EA31337/EA-Tester](https://github.com/EA31337/EA-Tester): headless backtesting image, about 1.1 GB;
    the MT5 tag is about 5 years old.
  - [PHUICMT/mcp-mt5](https://github.com/PHUICMT/mcp-mt5): wraps compile and `terminal64.exe
    /config:tester.ini` with `ShutdownTerminal=1`.
  VERIFIED: existence, sizes, dates.
- **Strategy tester.** Batch mode is driven by an ini file. Forum threads report silent failures and agent
  authorisation problems ([1](https://www.mql5.com/en/forum/361861),
  [2](https://www.mql5.com/en/forum/8965/372885)). VERIFIED that the reports exist. Price history normally
  comes from a broker server. INFERRED: offline custom symbols with imported bars might work, but this is
  unverified.
- **Licensing.** MetaQuotes forbids "modification of distribution packages" and says "only conventional
  distributions are allowed" (statement quoted by
  [Finance Magnates](https://www.financemagnates.com/forex/technology/3rd-party-developer-gets-red-card-as-metaquotes-re-acts/)).
  VERIFIED quote; secondary source. The installer (`mt5setup.exe`) is a web installer that downloads the
  current build. INFERRED: no versioned offline archive, so it cannot be pinned and must not be vendored.
- **Open-source MQL5 tooling.** There is no compiler or interpreter. Only tree-sitter grammars exist, for
  example [mskelton/tree-sitter-mql5](https://github.com/mskelton/tree-sitter-mql5) (ISC). They parse but
  do not execute. VERIFIED by GitHub search.

| | |
|---|---|
| Feasible strategy | (a) Real MT5 under Wine (Debian trixie `wine` 10.0): compile only is plausible; the tester is fragile and data-bound. Unpinnable, licence-hostile, needs the network at build. (b) **F1 port**: re-express the EA's decision logic (signals, position sizing, prop-firm daily-drawdown guard, session filters) as a Python module with a small event-driven bar loop in the fixture. Hidden tests replay synthetic OHLC/tick series whose expected trades come from running the reference (never stored) |
| Fidelity | (a) L4 compile, unreliable test. (b) **L0**. Lost: MQL5 semantics such as `OnTick` timing, `ArraySetAsSeries` indexing, netting versus hedging accounts, order filling modes, `SymbolInfo*` properties, broker spread, commission and slippage, and the tester's tick modelling. Kept: the trading rules the user actually corrected (for example 019ebac4's 3 % daily-drawdown rule). Avoid AGPL backtesting.py and GPL backtrader as dependencies (VERIFIED licences); write the loop in plain Python or pandas |
| Image size | (a) about 1.5 to 4 GB. (b) the Python base |
| Licensing | (a) MetaQuotes proprietary. (b) clean |
| E2B risks | (a) Wine needs Xvfb for the terminal; IA32 emulation is irrelevant for the 64-bit tools; GUI dialogs hang headless. (b) none |
| Verdict | **Not feasible faithfully.** (b) is an inspired (F1/F0) task and must be declared as such; F0 ships per policy. 3 seeds |

### 2.7 FPGA HDL

Debian versions, VERIFIED via madison on 2026-10-12. Bookworm is now `oldstable`, trixie is `stable`.

| package | bookworm | trixie | notes |
|---|---|---|---|
| iverilog | 11.0 | 12.0 | cocotb supports Icarus 11.0+ |
| verilator | 5.006 | 5.032 | **cocotb 2.1 needs Verilator 5.036+**, so build v5.0xx from source or use OSS CAD Suite |
| ghdl (mcode, llvm, gcc) | 2.0.0 | 5.0.1 | cocotb supports GHDL 2.0+; VPI only, so no 9-value access |
| yosys | 0.23 | 0.52 | |
| SymbiYosys | not in Debian | not in Debian | git tags `yosys-0.26` to `yosys-0.47` match yosys releases; pin a tag or commit |
| z3 / boolector | 4.8.12 / 1.5.118 | 4.13.3 / 1.5.118 | SMT back-ends for sby |
| gtkwave | 3.3.118 | 3.3.121 | not needed for grading |

- **cocotb** 2.1.0 (2026-08-30, BSD-3). manylinux2014 x86_64 wheels for cp310 to cp314 mean no compiler is
  needed to install it (Debian needs `make` and `libpython3-dev`)
  ([install](https://docs.cocotb.org/en/stable/install.html),
  [simulators](https://docs.cocotb.org/en/stable/simulator_support.html)). VERIFIED.
- cocotb writes **JUnit/xUnit `results.xml`** by default, overridable with `COCOTB_RESULTS_FILE`
  ([JUnit docs](https://docs.cocotb.org/en/development/junit.html)). That is directly consumable by
  `grade.py`. VERIFIED.
- **OSS CAD Suite** ([YosysHQ/oss-cad-suite-build](https://github.com/YosysHQ/oss-cad-suite-build), ISC):
  dated daily tarballs, `oss-cad-suite-linux-x64-20261011.tgz` is **713 MB**. It bundles Yosys, sby,
  Verilator, Icarus, GHDL, cocotb, solvers and Python, and needs no system packages. VERIFIED.

| | |
|---|---|
| Feasible strategy | cocotb testbenches in `tests/suites/` driving the agent's RTL on Icarus (Verilog) or GHDL (VHDL): reset, stimulus, scoreboard against a Python reference model, run per test. Optional Verilator for SystemVerilog-heavy designs. Optional SymbiYosys properties (assertions or cover) as extra hidden checks |
| Fidelity | **L4**: real event-driven simulation of the agent's HDL, black-box through ports. This is the natural "accepts any correct implementation" setting |
| Image size | Debian route: about 150 to 400 MB (INFERRED). OSS CAD Suite: about 2 GB unpacked (INFERRED from 713 MB) |
| Offline recipe | `apt-get install iverilog ghdl-mcode yosys z3` from a snapshot.debian.org date (§3.1), plus `pip download cocotb==2.1.0` into a wheelhouse and `pip install --no-index`. Verilator: build a pinned tag at image build (~10 to 20 min at 2 vCPU, INFERRED) or take a dated OSS CAD Suite tarball with sha256. Everything is redistributable (GPL tools ship with Debian source from snapshot) |
| Licensing | GPL-2 (iverilog, ghdl), LGPL-3 or Artistic-2 (verilator), ISC (yosys, sby, OSS CAD Suite), BSD-3 (cocotb) |
| E2B risks | Low. Icarus and GHDL-mcode simulate small designs in seconds. A Verilator C++ build of a mid-size design takes 10 to 60 s and 0.5 to 1.5 GB RAM with `-j2` (INFERRED) |
| Verdict | **Recover now** (no HDL seed in the current index; keep the toolchain ready for the older corpus's seeds) |

### 2.8 Jenkins Groovy pipelines

- **JenkinsPipelineUnit** ([jenkinsci/JenkinsPipelineUnit](https://github.com/jenkinsci/JenkinsPipelineUnit),
  MIT). Latest **1.31** (2026-07-01) on `repo.jenkins-ci.org`. Its POM pulls `groovy-all` 2.4.21 (the
  Groovy Jenkins itself embeds) and `groovy-cps`. VERIFIED.
  - Requirements: Java 21; it is "not currently compatible with Groovy 4".
  - Supports scripted (`BasePipelineTest`) and declarative (`DeclarativePipelineTest`) pipelines and shared
    libraries (`localSource`/`projectSource`).
  - `helper.addShMock`, `registerAllowedMethod`, `callStack`, `assertJobStatusSuccess`/`Failure`, and an
    experimental CPS-serialisation check.
  - Unknown steps throw, so plugin steps must be registered.
  VERIFIED (README).
- **jenkinsfile-runner** (MIT): last release `1.0-beta-32` in **2023-11**, so stale. VERIFIED.
- **Real Jenkins.** LTS is 2.580.1 (from the update-centre redirect). VERIFIED. The war plus plugins
  pinned with the plugin installation manager (INFERRED) can run a pipeline over loopback.

| | |
|---|---|
| Feasible strategy | JenkinsPipelineUnit tests (Gradle or Maven, JUnit XML) loading the agent's `Jenkinsfile` and shared-library vars. Assert on the stage, step and `when` decisions, `sh` invocations, environment propagation and the job result under mocked `sh` outcomes. Higher-fidelity option: a real Jenkins LTS with pinned plugins running the pipeline headless, with `sh` steps doing harmless local work |
| Fidelity | JPU: **L2** with the community-standard harness. Real Groovy execution of the pipeline, mocked steps; declarative-model fidelity is partial (INFERRED). Real Jenkins: L4 for orchestration, but SCM, credentials and agents cannot exist offline |
| Image size | JDK 21 (~300 MB) + Gradle or Maven + warm repository (~200 to 400 MB). Real Jenkins: +~0.5 to 1 GB with plugins (INFERRED) |
| Offline recipe | Warm `~/.m2` or `GRADLE_USER_HOME` at build by running the hidden suite's build once on the fixture (better than `dependency:go-offline`, which may miss surefire providers; the Maven docs only describe resolving declared plugins and deps, [go-offline](https://maven.apache.org/plugins/maven-dependency-plugin/go-offline-mojo.html)). Grade with `mvn -o` or `gradle --offline` |
| Licensing | MIT (JPU, Jenkins), Apache-2.0 (Groovy, Maven, Gradle) |
| E2B risks | Low. JVM start plus Gradle is 30 to 90 s cold (INFERRED) |
| Verdict | **Recover now**, once a JVM toolchain exists. 1 seed (019f92f5) |

### 2.9 Others

Debian package versions are VERIFIED via madison; the table states the rest.

| ecosystem | strategy | fidelity | size | offline recipe | licence | E2B risk | verdict |
|---|---|---|---|---|---|---|---|
| **R** (019f80c3) | `r-base-core` 4.2.2 (bookworm) or 4.5.0 (trixie); tests via `testthat`, or Python driving `Rscript` | L4 (native) | ~200 to 400 MB | apt from snapshot; CRAN packages pinned through a Posit Package Manager dated snapshot or vendored source tarballs (INFERRED) | GPL | low | **Recover now**: classifier bug, not an engine |
| **MATLAB to GNU Octave** (019ea7b5, 019f6ef1) | `octave` 7.3.0 or 9.4.0; pytest drives `octave-cli` | L4 if the code is Octave-compatible; MATLAB-only toolboxes break it | ~300 to 600 MB | apt from snapshot | GPL | low | **Recover now** where code is Octave-compatible. Fix the `.m` to `objc` mapping |
| **C# desktop (VS2015)** (019f4e99) | .NET 10 SDK building logic projects; or Mono 6.12 (trixie) for .NET Framework 4.x; WinForms not graded | L2 | ~0.7 to 1 GB | as in §2.2 | MIT | low | **Recover with effort** |
| **Factorio mod** (019ffb24) | real headless server plus [FactorioTest](https://github.com/GlassBricks/FactorioTest) (MIT) CLI | L4 | ~0.5 to 1 GB (INFERRED) | download a versioned headless tarball at build | Factorio ToS: "not allowed to copy, transfer or distribute the game" ([ToS](https://www.factorio.com/terms-of-service)), so no vendoring. INFERRED: Space Age DLC content in headless is unverified | low | **Recover with effort, legal-gated**; low priority |
| **WoW addon** (01a01bf4) | Lua 5.1 + busted on pure logic | L2, small | +5 MB | apt | MIT | low | low value; private-server context |
| **GoreBox mod** (019f1241) | none (proprietary Unity game, PlayFab) | n/a | n/a | n/a | proprietary | n/a | **Not feasible** |
| **Arduino / embedded** | `arduino-cli` (GPL-3.0, v1.5.x) with `core install arduino:avr@<ver>` at build; run the ELF in **simavr** (Debian 1.6) and assert on UART output or pin traces; or PlatformIO `pio test -e native` for host-side logic (pre-install packages with `PLATFORMIO_CORE_DIR`); QEMU user or system mode for ARM | simavr and QEMU: L4 CPU and peripheral emulation (real firmware); external sensors are not covered | +0.3 to 1 GB | cores and toolchains fetched at build, never at run time (arduino-cli downloads its index) | GPL / Apache | QEMU TCG needs no KVM. Do not rely on binfmt_misc in the guest; call `qemu-arm` explicitly (INFERRED) | **Recover now** when seeds appear |
| **Swift on Linux** | `swift:6.4.0-bookworm` (1.33 GB compressed, VERIFIED) or Debian trixie `swiftlang` 6.0.3; XCTest or swift-testing on Foundation code; no UIKit or SwiftUI | L4 for non-UI code | ~1.3 to 3 GB | SwiftPM: resolve at build into `.build/` and use `--disable-automatic-resolution` (INFERRED) | Apache-2.0 | moderate image size | **Recover with effort** (non-UI only) |
| **Electron** | Playwright `_electron.launch` (experimental, Electron v21+, [docs](https://playwright.dev/docs/api/class-electron)) under Xvfb with `--no-sandbox` | L4 | +~300 MB (Electron binary plus GTK and NSS libs) | the npm `electron` package downloads its binary; pre-seed the cache at build via `electron_config_cache` ([docs](https://www.electronjs.org/docs/latest/tutorial/installation)) | MIT | memory OK at 4 GiB | **Recover with effort** |
| **WebGL / canvas games** | the existing browser toolchain (Playwright Chromium). Canvas 2D works headless. WebGL needs `--use-gl=angle --use-angle=swiftshader-webgl --enable-unsafe-swiftshader`: automatic SwiftShader fallback is deprecated (since M130, effective around M139) and `--disable-gpu` breaks WebGL ([Chromium SwiftShader doc](https://chromium.googlesource.com/chromium/src/+/da170c2267b2201954efe1c8fa5d2d89ab09f7bf/docs/gpu/swiftshader.md), [blink-dev](https://groups.google.com/a/chromium.org/g/blink-dev/c/yhFguWS_3pM/m/oLrB5up_BwAJ)) | L4, CPU-rendered (slow but deterministic enough for state assertions; avoid pixel-exact asserts) | +0 over the current browser image | Playwright browsers baked at build (already done) | BSD / Apache | SwiftShader is CPU-heavy at 2 vCPU | **Recover now** (already in scope as `browser`) |
| **pygame** | `SDL_VIDEODRIVER=dummy`, `SDL_AUDIODRIVER=dummy` (or `disk`); drive the game loop with synthetic events | L4 for logic and state; no real window | pygame 2.6.1 wheel (LGPL) | pip wheelhouse | LGPL | low | **Recover now** (third-party sources: [bugnet](https://bugnet.io/blog/fix-pygame-event-pump-without-display-on-headless-server), [nixpkgs pygame-ce test env](https://git.uninsane.org/colin/nixpkgs/commit/0224fcf295cd3803e1ee1f66e27c6e94cd9eb703)) |
| **Audio** | WebAudio `OfflineAudioContext` in Chromium (deterministic); `--autoplay-policy=no-user-gesture-required`; PulseAudio `module-null-sink`, or an SDL or ALSA null device for native code (INFERRED) | L4 on the computed buffers | small | apt | various | low | **Recover now** |

---

## §3 General techniques for reproducible offline images

### 3.1 Pinning OS packages

- **Base image by digest**: `FROM debian:bookworm-20261001-slim@sha256:<digest>` (INFERRED syntax,
  standard). Note: E2B requires Debian-based templates (legacy docs say "Debian based, such as Ubuntu",
  [template file](https://e2b.dev/docs/sandbox/templates/template-file)). VERIFIED for legacy docs.
- **snapshot.debian.org.**

  ```
  deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/20261001T000000Z bookworm main
  deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/20261001T000000Z bookworm-security main
  ```

  Plus `Acquire::Check-Valid-Until "false";`. The
  [repro-sources-list.sh](https://github.com/reproducible-containers/repro-sources-list.sh) helper rewrites
  the sources to the snapshot matching the base image's sources-file timestamp and exports
  `SOURCE_DATE_EPOCH`. For Debian 13+ the project points to an `alternative/` method. VERIFIED.
- **Trixie's apt** adds `--snapshot` and `APT::Snapshot` for sources marked `Snapshot: enable`
  ([trixie notes](https://michael-prokop.at/blog/2025/07/20/what-to-expect-from-debian-trixie-newintrixie/)).
  VERIFIED.
- **Risk**: snapshot.debian.org throttles heavy users (INFERRED). Debian `main` .debs are redistributable,
  so cache them in `environment/deps/` when the build must not depend on the snapshot server's
  availability.

### 3.2 Language package managers

| ecosystem | recipe | source |
|---|---|---|
| pip | `pip download -d wheelhouse -r requirements.txt` (or `pip wheel`), then `pip install --no-index --find-links=wheelhouse -r requirements.txt`; add `--require-hashes` with `==` pins | [pip user guide](https://pip.pypa.io/en/stable/user_guide/), VERIFIED |
| npm | `npm ci` from the lockfile at build; at run time `--offline` ("no network requests will be done during install"); `--prefer-offline` still fetches missing data | [npm config](https://docs.npmjs.com/cli/v11/using-npm/config), VERIFIED |
| cargo | `cargo vendor vendor/` and paste the printed source replacement into `.cargo/config.toml`; build with `--frozen` (= `--locked` + `--offline`) | [cargo vendor](https://doc.rust-lang.org/cargo/commands/cargo-vendor.html), VERIFIED |
| go | `go mod vendor`; with go >= 1.14 and `vendor/` present, `-mod=vendor` is the default and "will not use the network or the module cache"; `GOPROXY=off` | [go modules ref](https://go.dev/ref/mod), VERIFIED |
| Gradle | warm `GRADLE_USER_HOME` by running the real build; grade with `--offline`; optional shared `GRADLE_RO_DEP_CACHE` (incubating); enable dependency verification | [Gradle caching](https://docs.gradle.org/current/userguide/dependency_caching.html), VERIFIED |
| Maven | `dependency:go-offline` resolves declared deps and plugins; run the real test phase once at build to catch lazily resolved providers; grade with `mvn -o` | [go-offline](https://maven.apache.org/plugins/maven-dependency-plugin/go-offline-mojo.html), VERIFIED (lazily resolved providers: INFERRED) |
| pub (Dart/Flutter) | `PUB_CACHE=/opt/deps/pub-cache flutter pub get` at build; `--offline` at run | [pub get](https://dart.dev/tools/pub/cmd/pub-get), VERIFIED |
| NuGet | `NUGET_PACKAGES=/opt/deps/nuget dotnet restore` at build; `dotnet test --no-restore` | INFERRED (standard) |
| single binaries (Godot, Luau, Lune, Rojo, OSS CAD Suite, Flutter) | download by exact versioned URL plus `sha256sum -c` in the Dockerfile; vendor into `environment/deps/` only when the licence allows (MIT, BSD, MPL, Apache, GPL with source) | INFERRED |

This workspace already does this for go and rust (`GOPROXY=off`, `CARGO_NET_OFFLINE=true` in
`toolchain.py`). New toolchains should follow the same shape: `/opt/deps/<tool>`, plus a `login_shell_path`
re-export so Codex's `sh -lc` keeps the PATH (CLAUDE.md gotcha C-033).

### 3.3 Verifying reproducibility

- `SOURCE_DATE_EPOCH` pins timestamps in BuildKit output
  ([Docker docs](https://docs.docker.com/build/ci/github-actions/reproducible-builds/)). VERIFIED.
  INFERRED: BuildKit's `rewrite-timestamp=true` exporter option also rewrites layer file timestamps.
- `diffoci diff --semantic IMG0 IMG1` compares two builds, ignoring timestamps, history and tar ordering;
  `--report-dir` dumps the conflicting files ([diffoci](https://github.com/reproducible-containers/diffoci)).
  VERIFIED.
- Behavioural reproducibility matters more than bit-for-bit images here. `shipcheck --image --repro`
  already rebuilds both images with `--no-cache` and checks reference = 1.0 and fixture = 0; that is the
  right acceptance test for every new toolchain.
- Small images: multi-stage builds that download and verify in a builder stage and copy only `/opt/<tool>`;
  `-slim` bases; `apt-get --no-install-recommends`; prune SDK artifacts for other platforms (Flutter
  android/ios, Godot export templates); do not ship `.git` histories. INFERRED, standard.

### 3.4 Firecracker / E2B specifics

- **E2B template build limits** ([docs](https://docs.e2b.dev/template/quickstart)), VERIFIED:
  - up to **1 hour** per build;
  - disk **10 GiB on Hobby, 20+ GiB on Pro**, custom on Enterprise;
  - 8 vCPU and 8 GiB on Hobby.

  Self-hosted limits may differ; confirm with the customer. Above 10 GiB rules out the Unity Editor and GMod
  in practice; Flutter and Android fit with pruning.
- **No `/dev/kvm`**: no Android emulator and no nested VMs. QEMU TCG, Wine, Xvfb and SwiftShader all run in
  user space (INFERRED, standard).
- **32-bit binaries** (old srcds, 32-bit Wine) need IA32 emulation in the guest kernel. Unverified for the
  customer's kernel; avoid.
- **Xvfb plus browser at 4 GiB.** Xvfb at 1280x720x24 is well under 100 MB. Chromium headless is about 200
  to 600 MB per page. Electron is about 300 to 600 MB. Use `--disable-dev-shm-usage` when `/dev/shm` is
  small, and `--no-sandbox` when running as root without user namespaces (INFERRED). Playwright's
  headless-shell Chromium needs no Xvfb; only Electron, real-window Chromium, Wine GUIs and SDL with real
  video drivers do.
- **CPU (2 vCPU)**: compile-heavy graders (Gradle/AGP, Flutter, Verilator) cost 30 s to 4 min per
  invocation. `grade.py` copies the tree fresh per check and runs the runner per check, so fewer, wider
  checks are cheaper for these toolchains (INFERRED from this repo's design).

---

## §4 Prioritized plan for recovering killed seeds

Each P-step is a toolchain plus a selection change. Each recovered seed must still pass the classifier's
other gates (a correction route, a reproduction) and the review. A runtime alone recovers nothing.

| priority | work | seeds unlocked (kaizen index) | verdict | why first |
|---|---|---|---|---|
| **P0a** | **Selection hygiene, no toolchain.** (1) Exclude cheat and exploit scripts with a policy kill (`exploit_or_cheat`): 019eed98, 019ea704, 019e9403, 019ff6be, 019e5bc7. (2) Fix language mapping: R is not engine (add an `r` toolchain), `.m` is MATLAB/Octave not objc, `groovy` and `java` are JVM | 0 (removes 5 bad seeds) | - | correctness of the funnel |
| **P0b** | **R/Octave toolchain**: Debian `r-base-core` or `octave` from snapshot, pytest-driven `Rscript`/`octave-cli` runners | 019f80c3, 019ea7b5, 019f6ef1 | Recover now | trivial, native |
| **P0c** | **Godot toolchain**: official binary pinned per seed (4.4.1 for 019f6f99), GUT or gdUnit4 vendored, `--headless --import` at build and at grade, JUnit out | 019e72b8, 019f03bd, 019f19ed, 019f6f99, 019ff0b5 | Recover now | L4 fidelity, MIT, about 200 MB, five seeds |
| **P0d** | **JVM toolchain** (JDK 21 + Gradle/Maven warm caches, `--offline`): first JenkinsPipelineUnit, then MockBukkit | 019f92f5 (Jenkins); 019e7c76, 019e8486, maybe 019eb0a9 (Paper/Velocity) | Recover now (Jenkins); with effort (plugins) | a shared base for P1b |
| **P1a** | **Flutter toolchain**: SDK pinned from the lockfile, pruned, warm pub cache, `flutter test --no-pub` with JSON reporter | 019fa93f, 019f52b1 | Recover now (image care) | L4, BSD |
| **P1b** | **Android on the JVM toolchain**: SDK fetched at build (licence review first), Robolectric offline jar, Gradle memory caps | 019e8b35, 019f73e8, 019f79e3, 019f880c | Recover with effort | four seeds; heaviest image and grader |
| **P1c** | **Roblox L3 toolchain**: luau, luau-lsp, Rojo and Lune pinned; a reviewed, generic, open-source-only require-glue for ModuleScripts; TAP or JUnit from a Lune harness. Run the semantic review on the first task to confirm L3 is not judged a fake runtime | 019f9775, then 019fced8, 019e9980 | Recover with effort | about 70 MB, MIT/MPL; fidelity bounded to logic |
| **P1d** | **HDL toolchain** (Icarus, GHDL, cocotb wheel; Verilator only if needed) | none in the current index; older-corpus HDL seeds | Recover now | cheapest L4 of all; ready for the next dump |
| **P2a** | **.NET toolchain** for engine-free C# (Unity logic, VS2015 desktop logic). Inspect each seed first; drop it if the corrected code touches `UnityEngine` | 019ecaa0, 019fbda1, 019f4e99 | Recover with effort, seed by seed | L2 only |
| **P2b** | **MQL5 as F1 Python ports** of the EA's decision logic | 019ebac4 (drawdown rule), 019fae87, 019e9823 | Not faithful; F1 inspired | delivery class decision |
| **P2c** | Factorio (headless server downloaded at build, FactorioTest), after a legal read of the ToS for evaluation use | 019ffb24 | With effort, legal-gated | one seed |
| **Do not pursue** | GMod srcds, Unity Editor, MT5 terminal tester, Android emulator, Minecraft Fabric/Forge/client mods, GoreBox, Roblox Studio, run-in-roblox, Open Cloud | 019ea725, 019ea7d5, 019eb9e5, 019ef532, 019f1241, 01a01bf4 (low value) | Not feasible | licence, pinning, network or KVM |

Rough yield, all 42 engine sessions accounted for:

| group | sessions | what |
|---|---:|---|
| P0, runtime that meets the constraints | 9 | R 1, Octave 2, Godot 5, Jenkins 1 |
| P1, runtime that meets the constraints | 9 | Flutter 2, Android 4, Roblox logic 3 |
| Paper/Velocity plugins on the JVM toolchain | up to 3 | |
| P2, L2 only or legal-gated | 4 | C# 3, Factorio 1 |
| F1-inspired only | 3 | MQL5 |
| not feasible | 9 | GMod 2, Fabric/client mods 2, GoreBox 1, WoW 1, engine-only Roblox 3 (viewport, prompt events, camera) |
| exploit or cheat, never build | 5 | |

How many of the recovered seeds survive the classifier's correction-route gate and the review is
**unmeasured**.

**Integration notes for this pipeline (INFERRED from the repo).**

- Add `Toolchain` entries in `pipeline/author/toolchain.py`. Remove the recovered languages from
  `NO_RUNTIME` and `config.ENGINE_LANGUAGES`, behind a per-ecosystem flag so the classifier can be re-run.
  Re-rank and record the new kill reasons.
- Every runner must emit JUnit, JSON or TAP so `grade.py` counts tests ("exit status is not evidence"):
  - GUT `-gjunit_xml_file`; gdUnit4 `results.xml`;
  - cocotb `results.xml`; Gradle and Maven surefire XML;
  - `flutter test --reporter json`;
  - a Lune harness writing TAP.
- Hidden suites live in `tests/suites/` and are copied into the grading tree:
  - GUT/gdUnit4 test scripts need `res://` placement, so copy them under the project root in the grading
    copy, not into the agent's repo;
  - Lune harness scripts get the repo path via `ARENA_REPO`.
- Rebuild each new toolchain with `arena shipcheck --image --repro` and run both `sandbox_smoke` variants
  before the first real build, as CLAUDE.md requires.

---

## §5 What is verified versus untested

**VERIFIED this session**:

- tool versions, licences, archive status and asset sizes (GitHub API, PyPI, Maven, Docker Hub, the
  Flutter and .NET indexes);
- Debian package versions in bookworm and trixie (madison);
- the Luau and Lune binaries' glibc requirements;
- Lune's Roblox API surface;
- Godot CLI flags, and the GUT and gdUnit4 CLIs and compatibility tables;
- GameCI activation options and Unity Personal's Hub-only activation;
- the Unity Reference-Only licence terms;
- the Android SDK §3.1, §3.4 and §3.5 text;
- Robolectric offline properties and the android-all licence;
- cocotb simulator minimums and its JUnit output;
- JenkinsPipelineUnit requirements;
- the MetaEditor CLI syntax;
- the Factorio ToS distribution clause;
- E2B template build limits;
- Chromium's SwiftShader deprecation and flags;
- the pip, npm, cargo, go, Gradle and pub offline mechanisms.

**INFERRED or untested**:

- every image-size figure marked INFERRED;
- every timing figure at 2 vCPU;
- whether Lune's `deserializePlace` exposes ModuleScript `Source` cleanly enough for a require-glue (high
  confidence, not executed);
- the reviewer's acceptance of L3 (Roblox via Lune) and of official framework test doubles;
- MT5 tester behaviour without a broker connection;
- srcds IA32 needs on the guest kernel;
- Flutter `precache`, `--no-pub` and the reporter flags;
- Robolectric with Compose under 4 GiB;
- Factorio headless DLC content;
- whether the customer's self-hosted E2B uses the cloud plan's disk limits.

None of the proposed toolchains has been built or run in this workspace.
