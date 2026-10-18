# W7: Harbor 0.20.0@cfc1bcb on self-hosted E2B: how our task packages hold up

Researched 2026-10-12. Sources: the Harbor source at the pinned commit, the e2b Python SDK (the 2.25.0 version Harbor's lockfile pins, plus the latest 2.55.1), the E2B infra source (`e2b-dev/infra` main @ `74558d3d`, 2026-10-12), the E2B guest-kernel configs (`e2b-dev/fc-kernels` @ `82a58ea8`), E2B docs, Harbor GitHub issues and PRs, and our 26 delivered zips in `deliverable-zips-coding-v2/`. I did not modify any pipeline file. Scratch work lives in `/tmp/w7r/` (clones, SDK venvs, probe script).

Labels used below:
- **VERIFIED (code)**: I read it in the pinned source.
- **VERIFIED (doc)**: I read it in official docs or a primary issue or PR.
- **VERIFIED (exec)**: I ran it, and the command and output are shown.
- **INFERRED**: reasoned from verified facts but not executed end to end.

We do not know which E2B infra version the customer self-hosts. Every infra statement is against current main, and older self-hosted deployments may differ in detail.

---

## 0. Bottom line

1. **If the customer uses Harbor's stock `-e e2b` provider (most likely), none of our 26 delivered tasks can start.** Harbor at cfc1bcb turns `environment/Dockerfile` into an E2B template with the e2b SDK's `Template().from_dockerfile()`. That parser raises `Multi-stage Dockerfiles are not supported`. All 26 agent Dockerfiles are multi-stage (`FROM node… AS node` plus `COPY --from=node`), and so are 8 of the 26 verifier Dockerfiles. I confirmed this by running the exact SDK version Harbor pins (2.25.0) and the latest (2.55.1) against every shipped Dockerfile (§6). The trial errors before the agent runs, so the oracle gets no 1.0 and NOP gets no 0.0. Our Docker validation cannot see this: Docker builds multi-stage files natively.
2. Fixing multi-stage alone is not enough. Three more defects each fail the template build:
   - **`.dockerignore` negation (`!repo/**`).** The SDK version Harbor locks (2.25.0) does not support it, so `COPY deps/` matches no files ("No files found"). This affects 26 of 26 tasks (VERIFIED exec). SDK 2.55.1 handles it.
   - **`useradd --uid 1000 agent`.** E2B's builder creates its own user `user` before our steps run, and on our python and golang bases that user takes UID 1000. The build then fails with "UID 1000 is not unique". This affects 19 of 26 tasks (VERIFIED code; the UID number is INFERRED from Debian defaults).
   - **E2B runs every build `RUN` as `/bin/bash -l -c`.** On Debian bases, `/etc/profile` resets `PATH`, so tools found only through image `ENV PATH` disappear. In the Go task, `go mod download` would fail in both images (VERIFIED code and Debian source).
3. **E2B does not apply image or Dockerfile `ENV` when commands run.** Only `PATH`, `HOME`, `USER`, `LOGNAME`, `E2B_*` and Harbor's own variables arrive, and `bash -l` then resets `PATH`. Our Python and Node graders survive because `grade.py` gets the interpreter `PATH` from `checks.json`. The Go grader does not: its toolchain env has no `PATH`, so `go` (in `/usr/local/go/bin`) is not found (VERIFIED code). The agent's `/etc/profile.d` PATH file would be captured *after* the reset during an E2B build, so it would not contain `/opt/deps/venv/bin` (INFERRED from verified pieces).
4. **The verifier sandbox may get 1 GiB of RAM.** Our `[verifier.environment]` sets only `network_mode`. Unless the customer passes `--override-cpus/--override-memory-mb`, Harbor builds the verifier template with the SDK defaults, `cpu_count=2, memory_mb=1024` (VERIFIED code). The 2 vCPU / 4096 MB the customer quotes reaches the verifier only through those overrides.
5. **A separate issue may also affect Docker.** In 11 Node tasks, `/app/repo/node_modules` is an absolute symlink to `/opt/deps/node_modules`. Harbor collects `/app/repo` (it has `exclude`) with `tar` and unpacks it on the host with `tarfile.extractall(filter="data")`. That call raises `AbsoluteLinkError` at the symlink and stops unpacking, so the uploaded repo is cut short at an order-dependent point (VERIFIED exec of the tarfile behaviour). E2B's ext4 rootfs can order directory entries differently from a Docker host, so this can pass on Docker and fail on E2B. One check settles it: the `/app/repo` entry in `artifacts/manifest.json` of the existing Docker oracle runs. Excluding `node_modules` from the artifact removes the risk on both runtimes.
6. **NOP risk is small; oracle risk is large.** Every E2B difference found pushes toward errors or false zeros. None pushes a NOP above 0.
7. **Best ways to validate:**
   - (a) Today, at no cost: run the real e2b SDK parser offline in shipcheck (script in Appendix A).
   - (b) Run Harbor@cfc1bcb with `-e e2b` against E2B cloud on a **Pro** key. Hobby rejects Harbor's hard-coded 24 h sandbox timeout (issue #1113).
   - (c) Optionally, run a local self-hosted E2B on a KVM-capable Linux VM.
   - (d) A Docker "E2B-sim" (login-shell exec, environment wiped, flattened uploads, injected `user` account) is a cheap approximation (§8).

---

## 1. What cfc1bcb actually is

- **VERIFIED.** `cfc1bcb9480f123b0a702a1428ae3f79ecedb0ca` (2026-09-06) is **not on `main`** and not inside any tag. It sits on branch `harbor-miles-v0.20.0`: tag `v0.20.0` (`459ff6ec`, 2026-07-18) plus 43 "miles" commits (Daytona RL rollout, agent-server, Claude Code config, phase-scoped allowlists). `pyproject.toml` still reads `version = "0.20.0"`. The repo moved from `laude-institute/harbor` to `github.com/harbor-framework/harbor`.
- **VERIFIED.** After the pin, the same branch gained E2B fixes the customer does **not** have:
  - #3372, reuse prebuilt templates (`prebuilt_template_id`), 09-22
  - #3389, sandbox TTL lowered from 24 h to 8 h, 09-23
  - #3413, renewable 1 h lease, 09-29
  - #3414, create retries, 09-29
  - #3499, dispatch retries, 10-02

  Branch head: `917b3c1b`. `git diff cfc1bcb origin/harbor-miles-v0.20.0 -- src/harbor/environments/e2b.py` shows 96 changed lines.
- **VERIFIED.** Several `shi/*-e2b-*` branches build on cfc1bcb. One adds "Include requested resources in E2B template cache identity" (`20e3963d`), with the comment *"CPU and memory are baked into E2B templates, so a resource override must not reuse a template built for a different machine size."* That is the cache bug that exists at the pin (§4).
- **VERIFIED (code).** `uv.lock` at cfc1bcb pins `e2b==2.25.0`, and `pyproject.toml` declares the extra `e2b = ["e2b>=2.25.0", "dockerfile-parse>=2.0.1"]` (line 69). Customers installing with `pip install harbor[e2b]` instead of `uv sync` get the newest SDK. This matters for the `.dockerignore` finding.

## 2. Q1: task schema support at cfc1bcb (`src/harbor/models/task/config.py`)

None of the models set `extra="forbid"`, so pydantic v2 **ignores unknown keys silently** (VERIFIED code: no `ConfigDict(extra=…)` in `models/task/*.py` except frozen ids).

| Our field | Status at cfc1bcb | Evidence / note |
|---|---|---|
| `schema_version = "1.4"` | Accepted as a free string (default `"1.3"`). Nothing reads it. | `config.py:791`. Grepping `src/` finds no consumer of the task `schema_version`. |
| `[[steps]]` | Supported. `Trial.create` → `MultiStepTrial` when `task.has_steps`. | `config.py:754-780`, `trial/trial.py:259-275`, `trial/multi_step.py` |
| `multi_step_reward_strategy = "mean"` | Supported (`mean`/`final`). `mean` = per-key mean over steps that have a verifier_result. Steps with none are left out of the denominator. | `config.py:783-813`, `multi_step.py:236-260` |
| `[[artifacts]] source/exclude` | Supported. `exclude` becomes `tar --exclude` flags. | `config.py:634-718`, `artifact_handler.py:311-326`, `environments/base.py:972-1027` |
| `[verifier] environment_mode = "separate"` + `[verifier.environment]` | Supported for **every** provider. The verifier env is created by the same factory with the trial's environment type, so on `-e e2b` it is a second E2B sandbox built from `tests/` (or `steps/<s>/tests/`). | `config.py:549-605`, `trial/trial.py:556-689`, `models/task/verifier_mode.py:43-64` |
| `network_mode` values | `no-network`, `public`, `allowlist` | `config.py:36-41`. E2B declares `disable_internet`, allowlists and dynamic switching (`e2b.py:125-138`). |
| `[agent] user = "agent"` | Supported. The agent setup and run phases use `with_default_user(agent.user)`. On E2B it is passed to envd as the username. | `config.py:334-339`, `trial.py:388`, `trial.py:440`, `e2b.py:484-493` |
| `[agent] network_mode`, `[verifier] network_mode` | Explicit phase policies. Ours equal the baselines (`no-network`), so no runtime `update_network` switch happens. | `trial/network_policy.py`, `trial.py:146-217` |
| `[steps.verifier] timeout_sec/env` | Supported. A step **always** uses its own verifier timeout (the model default is 600.0, never None), so task-level `[verifier] timeout_sec` is ignored for steps. We set 2040-2400 per step. | `config.py:557`, `multi_step.py:456-466` |
| `[environment] build_timeout_sec` | Supported. It bounds `env.start()` (on E2B: template build + sandbox create) for the agent env **and** the separate verifier env. | `trial.py:1091-1095`, `trial.py:1169-1180`, `trial.py:656-660` |
| `cpus`, `memory_mb` | Supported. On E2B they are **template build** parameters (`AsyncTemplate.build(cpu_count, memory_mb)`). Not set in our `[verifier.environment]` → see §4 R5. | `e2b.py:192-214` |
| `[task] version = "1.0.0"` | **Ignored.** `PackageInfo` has only name, description, authors, keywords. | `config.py:285-327` |
| `[metadata]` | Free dict | `config.py:796` |
| `os = "linux"` | Supported | `config.py:271-275` |

Nothing in our toml is rejected. The silently ignored or overridden items are `[task] version`, task-level `[verifier] timeout_sec` for steps, and `schema_version` semantics.

## 3. Q2: Harbor's E2B provider at cfc1bcb (`src/harbor/environments/e2b.py`, 507 lines)

### 3.1 Template from Dockerfile (VERIFIED code)
- `_create_template` (`e2b.py:180-214`):
  - If `[environment].docker_image` is set, it uses `Template().from_image(image)`.
  - Otherwise it uses `Template(file_context_path=environment_dir).from_dockerfile(environment/Dockerfile)`, then `AsyncTemplate.build(template, alias=..., cpu_count=cpus, memory_mb=memory_mb)`. Each resource argument is passed only when set.
- **There is no Docker build anywhere.** The e2b SDK parses the Dockerfile into E2B "steps", and E2B's template-manager runs them inside a Firecracker build VM (§5.2).
- Template alias = `f"{environment_name}__{environment_id[:12]}"`, with `/`→`__` and `.`→`-` (`e2b.py:105-108`). `environment_id` is a content hash of the env dir's file **bytes and paths only**: modes are ignored, as are `.git` and `__pycache__` (`definition.py:81-111`). CPU and memory are **not** part of the alias (§7).
- `start()` (`e2b.py:254-269`): if the alias does not exist (or `force_build`), build; then create the sandbox, `ensure_dirs(mount targets)` as root with `chmod 777`, and upload `environment/` only for prebuilt-image tasks without a Dockerfile. **There is no lock**: concurrent trials of a not-yet-built task each start a build of the same alias.
- E2B alias rule: lowercased, `^[a-z0-9-_]+$` (`infra/packages/shared/pkg/id/id.go:17`). Our `a_f*_slug` names comply.

### 3.2 Sandbox create, resources, network, timeouts (VERIFIED code)
- `AsyncSandbox.create(template=alias, metadata, envs=_startup_env(), timeout=86_400, allow_internet_access=(mode != no-network), network=allowlist opts only)` (`e2b.py:221-236`).
- The 24 h lifetime is hard-coded. A team or tier whose max sandbox length is shorter rejects creation with HTTP 400. E2B cloud Hobby allows 1 h (issue #1113, pricing page).
- `envs` = task `[environment].env` plus persistent trial env. These become envd **default** env vars (`infra …/envd/internal/api/init.go:447-450`). Ours is empty.
- Network: `no-network` → `allow_internet_access=False`, enforced host-side by E2B's per-sandbox firewall (`infra …/orchestrator/pkg/sandbox/network/firewall.go`, `DenyEgress`). Loopback inside the VM is unaffected. Allowlists use `allow_out` plus `deny_out=[ALL_TRAFFIC]`, and runtime switches use `update_network()` (`e2b.py:140-162`, `e2b.py:244-248`).
- Run-time `--allow-environment-host X` turns a `no-network` baseline into `allowlist [X]` for the agent env only. It does not apply to our verifier, because we declare `[verifier.environment]` (`network_policy.py:20-45, 66-84`). Because our `[agent] network_mode="no-network"` is explicit, that would force a dynamic switch, which needs the E2B network-update API on their infra (INFERRED, applies only if they pass such flags).
- Resources: `resource_capabilities = cpu_request, memory_request` (`e2b.py:117-121`). There is no disk or GPU knob. Disk is the E2B team/tier `disk_mb`.

### 3.3 Exec: user, shell, env (VERIFIED code)
- `exec()` → `sandbox.commands.run(cmd, background=True, cwd=…, envs=…, timeout=timeout_sec or 0, user=…)`, then `handle.wait()` (`e2b.py:438-507`).
- **User:** explicit user, else `default_user`, else **`"root"`** (`e2b.py:492`). On Docker, `None` means the image's `USER`. Harbor passes `agent.user` explicitly for agent phases and `"root"` for chmod, tar and dir resets. So for our packages the effective users match Docker: the agent runs as `agent`, the verifier as root, and `grade.py` drops to `grader`.
- **Shell:** the SDK always sends `cmd="/bin/bash", args=["-l","-c",cmd]` (`e2b/sandbox_async/commands/command.py:252-254` in 2.25.0; line 310 in 2.55.1). envd wraps that in `/bin/sh -c 'echo 100 > /proc/$$/oom_score_adj && exec [ionice] [nice] "$@"'` (`infra …/envd/internal/services/process/handler/handler.go:211-213`).
- **Process environment:**
  - `PATH` = **envd's own** PATH, then `HOME`, `USER`, `LOGNAME`, then envd defaults (sandbox-create `envs`, `E2B_*`), then the per-command `envs` (`handler.go:255-275`).
  - Image `ENV` and Dockerfile `ENV` are **not** included. The SDK says so: *"Environment variables defined here are available only during template build."* (`e2b/template/main.py:641`; E2B docs "Defining template" carry the same warning).
  - E2B engineer Matt Teufel's PR #3462 confirms it from probes: *"E2B does not pass image config ENV or Dockerfile ENV to `commands.run`. Only `E2B_*`, HOME, USER, LOGNAME, PATH and PWD arrive."*
- **Login shell:** Debian's `/etc/profile` sets `PATH` unconditionally. For root: `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`. For other users: `/usr/local/bin:/usr/bin:/bin:/usr/local/games:/usr/games`. Then it sources `/etc/profile.d/*.sh` ([Debian base-files 12.4+deb12u15 share/profile](https://sources.debian.org/data/main/b/base-files/12.4%2Bdeb12u15/share/profile)). E2B provisioning also appends `source ~/.bashrc; source ~/.profile` to `/etc/profile` (`provision.sh:94`). Ubuntu's `/etc/profile` does not set PATH.

### 3.4 File transfer (VERIFIED code)

**`upload_file`**
- Calls `sandbox.files.write(target, bytes)` (`e2b.py:305-316`). No user is passed, so envd writes as the **template default user**. With envd ≥ 0.4.0 that is the Dockerfile's last `USER`, or E2B's `user` when the Dockerfile has no `USER` (`envd/internal/api/upload.go:263`; `e2b/envd/versions.py:6`).
- envd creates missing parent dirs with mode 0755, owned by that user. It opens files with mode `0o666` (so 0644 after umask) and chowns them to that user (`upload.go:59-111`, `permissions/path.go:68-90`).

**`upload_dir`** (`e2b.py:324-351`)
- Walks `Path(source).rglob("*")`, keeps only `is_file()` entries, and writes them in batches of 20 through `files.write_files`.
- Compared with Docker's `docker compose cp`, this **drops executable bits, symlinks (a symlink to a file becomes a regular-file copy), empty directories and ownership**. Files belong to the sandbox default user.
- PR #3462 lists the same thing as root cause 4.

**Downloads**
- `download_file` uses `files.read(format="bytes")`.
- `download_dir` recurses with `files.list` (depth 1) as the default user and downloads file by file (`e2b.py:359-416`).
- Artifacts with `exclude` (our `/app/repo`) use the base-class path instead: root runs `tar czf /tmp/.hb-transfer-*.tar.gz --exclude=… -C /app/repo .` with a **120 s timeout**, downloads the tarball, and unpacks it with `tarfile.extractall(filter="data")` (`base.py:972-1027`). This path is **identical on Docker**.

### 3.5 Separate verifier on E2B (VERIFIED code)
- **Supported.** `_separate_verifier_env` (`trial.py:627-666`):
  - Builds an E2B environment with `environment_dir = tests/` (or `steps/<s>/tests/`) and `task_env_config = [verifier.environment]`.
  - Copies the trial's environment config, so `--override-cpus/--override-memory-mb` apply.
  - Uses mount target `/logs/verifier` (treated as a "mkdir hint" on E2B).
  - Starts within `build_timeout_sec`.
- The verifier template is built **lazily at the first verification** of each task. For real agents that happens after up to 3 h of agent time.
- In the sandbox (`trial.py:556-625`): as root, `empty_dirs([/logs/verifier])`, then `upload_artifacts`. For each artifact dir: `empty_dirs([source], chmod 777)`, then `upload_dir` to the **original path** (`/app/repo`, `/app/deliverables`). Files are written as the verifier template's default user (§3.4).
- Then `Verifier(skip_tests_upload=True)` relies on `/tests` baked into the image. It runs `chmod +x /tests/test.sh` as root, then `/tests/test.sh > /logs/verifier/test-stdout.txt 2>&1` as the verifier user (root here), and downloads `/logs/verifier` because E2B is not "mounted" (`verifier/verifier.py:96-103, 191-225`).

### 3.6 The customer's "Docker compatibility layer" (INFERRED)
At the pin Harbor has **no component by that name** for E2B. Searching `src/harbor/environments` for "compat" finds nothing relevant. The only Docker-related E2B path is the Dockerfile→template conversion above, which E2B documents as "Dockerfile" support ([docs.e2b.dev/template/base-image](https://docs.e2b.dev/template/base-image)). Plausible readings:
- (a) Stock `-e e2b` (`from_dockerfile`). This is most likely, and all §6 failures apply.
- (b) They `docker build` each `environment/Dockerfile` themselves and use `[environment].docker_image` / `from_image`, or a prebuilt template. Then multi-stage, `.dockerignore` and the build-time PATH problems disappear for the **agent** image. The runtime ENV loss, the E2B `user` account, upload flattening and the **verifier** `from_dockerfile` problems remain, unless they pre-build the verifier too.
- (c) A custom provider through `--env module:Class`.

**We should ask the customer which one, and which e2b SDK and E2B infra versions they run.**

## 4. Q3: E2B semantics that matter for us

### 4.1 Dockerfile → template conversion (e2b SDK)
**VERIFIED (code + exec)** for 2.25.0 (`e2b/template/dockerfile_parser.py`), with 2.55.1 differences noted:

| Dockerfile feature | e2b 2.25.0 | e2b 2.55.1 |
|---|---|---|
| >1 `FROM` (multi-stage) | `ValueError: Multi-stage Dockerfiles are not supported` (`:85-96`) | `DockerfileSyntaxError`, same message (`:240`). `COPY --from` explicitly unsupported (`:442`). |
| `FROM … AS x` / `--platform=` | alias split only on lowercase `" as "`. `--platform` is passed verbatim and breaks the image reference (Harbor PR #1452) | rewritten parser |
| `RUN` | `run_cmd` as the current user. Builder runs it as `/bin/bash -l -c` with build-time env (§5.2). | same |
| `COPY/ADD` | flags other than `--chown` silently dropped (`--chmod` ignored, `--from` treated as a local path). Absolute sources rejected. | `--chown`, `--chmod`, heredoc supported. `--from` rejected. |
| `.dockerignore` | patterns passed to `wcmatch.glob(exclude=…)` **without NEGATE**, so `!pattern` lines are literal and do not re-include (`utils.py:105-161`) | negation supported (`utils.py:149-165`) |
| `ENV`/`ARG` | `set_envs` → **build-time only** (`main.py:641`) | same (`main.py:630`) |
| `USER` | `set_user`. Starts as `root` and `/`. **If the file never sets USER, the SDK appends `USER user`; with no WORKDIR, `/home/user`** (`:107-141`) | same (`:268`) |
| `CMD`/`ENTRYPOINT` | start cmd = JSON array joined with spaces (loses quoting, PR #1452). Ready cmd = `sleep 20`. | rewritten |
| `EXPOSE`, `VOLUME`, `SHELL`, `HEALTHCHECK`, … | printed "Unsupported instruction" and skipped | skipped |
| Base image | must be Debian/Ubuntu, Fedora/RHEL, Arch or Alpine with `/etc/os-release` ([docs](https://docs.e2b.dev/template/base-image)) | same |

### 4.2 Template build pipeline (E2B infra, VERIFIED code)
Phase order is fixed (`orchestrator/pkg/template/build/builder.go:410-433`):

1. **base**: pull the OCI image and convert it to ext4. The base rootfs limit is 25 000 MB (flag `build-base-rootfs-size-limit-mb`). Image `ENV` is recorded into **build** metadata only (`phases/base/builder.go:186`). Then `provision.sh` runs **with unrestricted network** (`builder.go:191`, "nil network = no restrictions"):
   - installs `systemd systemd-sysv openssh-server sudo chrony socat curl ca-certificates fuse3 iptables git nfs-common less nftables iputils-ping jq iproute2` with `apt-get` from the image's **own** sources, i.e. `deb.debian.org` / `archive.ubuntu.com`, before our snapshot sources exist (`phases/base/distro/distro.go:49-66`);
   - runs `passwd -d root` (`provision.sh:100`);
   - configures sshd with `PermitRootLogin yes`, `PermitEmptyPasswords yes` (`:136-140`) and enables `ssh.service` (`distro/init.go`);
   - appends `.bashrc`/`.profile` sourcing to `/etc/profile` (`:94`);
   - sets `inotify max_user_watches=65536` and links systemd as init.
2. **user**: `USER user true`, i.e. `useradd --create-home --shell /bin/bash user`, plus the sudo group and `NOPASSWD` sudoers (`phases/user/builder.go:57`, `commands/user.go:63-65, 87-150`). It runs **before any of our steps**. On an image with no UID ≥ 1000 (python:3.12-slim-bookworm, golang:1.23-bookworm), Debian `useradd` assigns UID 1000 (INFERRED from Debian's `UID_MIN`).
3. **steps**: our instructions, each executed inside the build VM through envd as `/bin/bash -l -c <cmd>` with `Envs = image ENV + Dockerfile ENV`, where PATH gets `:/usr/local/sbin:…:/bin` appended (`sandboxtools/command.go:141-157`). On Debian the login shell's `/etc/profile` **overwrites PATH** before the command runs. Tools found only through image `ENV PATH` (`/usr/local/go/bin`, `/usr/local/cargo/bin`, a venv) are missing in `RUN`. Harbor PR #1452 records E2B failures from the same cause: *"base-image ENV not inherited (crustbench missing PATH)"*.
4. **finalize**: boot with systemd, run the configuration script, then the start command (`sleep infinity`) and ready command (`sleep 20`). Snapshot memory and disk. envd `DefaultUser` = the last `USER`, `DefaultWorkdir` = the last `WORKDIR` (`phases/finalize/builder.go:183-198`). Sandboxes later **resume from this snapshot**.

### 4.3 Guest runtime (VERIFIED code unless marked)

| Topic | E2B behaviour | Versus Docker (`--cpus 2 --memory 4g`, default caps) |
|---|---|---|
| PID 1 / init | systemd on Debian/Ubuntu (envd is a systemd unit, `core/rootfs/files/envd.service.tpl`); chrony, sshd and journald run | Docker: `sleep infinity` as PID 1, no systemd |
| Root capabilities | It is a VM, and envd runs commands as root with a full capability set. setuid/setgid/chown work. | Docker default caps include CHOWN, SETUID, SETGID. E2B is more permissive. |
| Kernel | default `vmlinux-6.1.158` (`shared/pkg/featureflags/flags.go:970`). `CONFIG_USER_NS=y`, `SECCOMP(_FILTER)=y`, `SECURITY_LANDLOCK=y`, `CGROUPS/MEMCG/CPUSETS/PIDS=y`, `NF_TABLES=y`, `IP_NF_IPTABLES=y`, `OVERLAY_FS=y`, `FUSE_FS=y`, `SYSVIPC/POSIX_MQUEUE=y`, `IO_URING=y`; `NFT_FIB_INET` absent, `XT_MATCH_OWNER` off, AppArmor off (`fc-kernels configs/x86_64/6.1.158.config`) | No Docker seccomp profile inside the VM, so ptrace, unshare and user namespaces are available (INFERRED from the config) |
| `/dev/shm` | systemd tmpfs, default about 50 % of RAM (INFERRED: standard systemd) | Docker default 64 MB. Chromium is safer on E2B. |
| `/tmp` | plain rootfs dir. `systemd-tmpfiles` wipes it at boot (`envd.service.tpl` comment). Build-time files in `/tmp` do not survive. | persists in the image |
| Memory / OOM | the VM's RAM = `memory_mb` **including** guest kernel, systemd, envd, chrony and sshd (INFERRED overhead of about 150-300 MB). **No swap** is configured (grep finds no `mkswap`/`swapon`). User processes get `oom_score_adj=100`, envd `-1000` (`handler.go:211`, `envd.service.tpl`). | Docker `--memory 4g` limits only container processes |
| Locale / TZ | `LANG` is not set at runtime because image ENV is dropped. Python 3.7+ coerces C to UTF-8 (PEP 538/540). Timezone is the image default (UTC). | the python images set `LANG=C.UTF-8` |
| ulimit nofile | children of envd (a Go program) get the systemd default soft limit, typically 1024 (INFERRED) | Docker daemon default (often 1048576, or 1024:524288 on newer engines) |
| Clock | chrony; envd corrects the clock on `/init` after snapshot resume | host clock |
| Extra packages and accounts | `git`, `curl`, `jq`, `sudo`, `openssh-server`, `iptables`/`nftables` are present in **every** image. There is a `user` account with NOPASSWD sudo. Root's password is **empty**. | absent unless we install them |
| Privilege boundary | Debian PAM `pam_unix nullok` is the default ([pam 1.5.2-6+deb12u2 pam-configs/unix](https://sources.debian.org/data/main/p/pam/1.5.2-6%2Bdeb12u2/debian/pam-configs/unix)). With root's password deleted, `su` to root without a password, or `ssh root@localhost` (empty password permitted), should work for the `agent` user (INFERRED, not executed). Running the agent as non-root is **not** a security boundary on E2B. The separate verifier still isolates grading. | Docker: root password locked, no sshd |
| Disk | tier/team `disk_mb` (E2B cloud: Hobby 10 GiB, Pro 20 GiB). Self-hosted: operator-defined. | host disk |

## 5. Q4: known Harbor and E2B pitfalls (Docker passes, E2B differs)

| Ref | State | What it says | Hits us? |
|---|---|---|---|
| [harbor#1452](https://github.com/harbor-framework/harbor/pull/1452) (open) | doc | e2b parser bugs: `FROM --platform`, JSON-array CMD quoting. The 46-benchmark NOP pilot also hit `ValueError: Multi-stage Dockerfiles are not supported` (swtbench-verified, binary-audit) and "base-image ENV not inherited (crustbench missing PATH)". | Yes: multi-stage (26 of 26), PATH (Go) |
| [harbor#3462](https://github.com/harbor-framework/harbor/pull/3462) (open, by an E2B engineer) | doc | four root causes: ENV not passed to `commands.run`; SDK appends `USER user`; envd uses `bash -l -c` and Debian `/etc/profile` resets PATH; `upload_dir`/`download_dir` drop modes, symlinks and empty dirs and run as the sandbox default user. The fix captures the image env and uses tar transfers. | Yes, all four |
| [harbor#3461](https://github.com/harbor-framework/harbor/pull/3461) (open) | doc | E2B fixes CPU and memory at build time while Harbor's alias leaves them out, so variants of one Dockerfile collide on one alias. There is no build lock: concurrent builds, 409s, the team concurrent-build limit (429). | Yes, if trials run concurrently on a cold template, or after any resource change |
| [harbor#3522](https://github.com/harbor-framework/harbor/pull/3522) (open) | doc | HTTP/2 edge drops end the stream of every long command on that connection. `exec()` raises even though the process keeps running (12 agents failed at one instant in a 490-concurrent run). | Possible for long verifications (ours run 30-40 min) |
| [harbor#3460](https://github.com/harbor-framework/harbor/pull/3460) (open) | doc | e2b < 2.52 shares 4 HTTP/2 pools, so about 400 concurrent commands exhaust them ("stream didn't open within request_timeout"). Brings the miles-branch fixes to main. | at scale |
| [harbor#1113](https://github.com/harbor-framework/harbor/issues/1113) (open) | doc | hard-coded `timeout=86400` → HTTP 400 on plans with a 1 h max | E2B cloud Hobby; any self-hosted tier with `max_length_hours < 24` |
| #3389 / #3413 / #3414 / #3372 / #3499 (merged into `harbor-miles-v0.20.0` **after** the pin) | doc | TTL, lease, create-retry, prebuilt-template and dispatch-retry fixes | the customer's pin lacks them |
| [harbor#2698](https://github.com/harbor-framework/harbor/pull/2698) (open) | doc | `upload_file` reads whole files into memory | not for us (small repos) |
| [harbor#598](https://github.com/harbor-framework/harbor/issues/598) (closed) | doc | the switch from `bash -ic` to `bash -lc` broke tasks that relied on rc files | context for the login-shell PATH problem |
| Our own C-033 (CLAUDE.md) | doc | Codex `sh -lc` + Debian `/etc/profile` lost the venv PATH, fixed with `/etc/profile.d` | the same mechanism, now during E2B **build** too |

## 6. Probes executed on our 26 delivered packages

All were run offline: no API key, no network beyond PyPI for the SDK.

**Setup:**
```bash
uv venv /tmp/w7r/uvenv/.venv --python 3.12
uv pip install --python /tmp/w7r/uvenv/.venv/bin/python "e2b==2.25.0" "dockerfile-parse>=2.0.1"
# probe = Appendix A: Template(file_context_path=dir).from_dockerfile(Dockerfile) then Template.to_json()
/tmp/w7r/uvenv/.venv/bin/python -I /tmp/w7r/scripts/e2b_parse_probe.py <task>/environment
/tmp/w7r/uvenv/.venv/bin/python -I /tmp/w7r/scripts/e2b_parse_probe.py <task>/tests
```

**Results (VERIFIED exec):**
- **Agent Dockerfiles: 26 of 26** → `PARSE_ERROR ValueError: Multi-stage Dockerfiles are not supported`. The same result under e2b 2.55.1 (`DockerfileSyntaxError`).
- **Verifier Dockerfiles: 8 of 26 rejected the same way.** These are the Node-family tasks that use `FROM python… AS certificates` plus `COPY --from=certificates …ca-certificates.crt`: shopify-theme-vue, vanilla-js-web-app, browser-rts-game, express-chat-app, express-glb-viewer, node-http-api, node-http-server, vanilla-js-webapp. The other 18 parse. The SDK appends `USER user` because they have no `USER`.
- **Single-stage variant of cli-tool's agent Dockerfile** (node stage removed), with the shipped `environment/.dockerignore` (`*`, `!Dockerfile`, `!repo/`, `!repo/**`, `!deps/`, `!deps/**`):
  - e2b 2.25.0: `SERIALIZE_ERROR ValueError: No files found in …/env/deps/`.
  - e2b 2.55.1: parses.
  - Without the `.dockerignore`, 2.25.0 also parses, and the step list shows `RUN useradd --create-home --uid 1000 agent` executed after E2B's own `user` creation (§4.2).
- **Survey (VERIFIED by reading every Dockerfile):**
  - Only FROM, RUN, COPY, ENV, USER, WORKDIR and CMD are used. There is no ARG, ENTRYPOINT, SHELL or BuildKit flag.
  - All 26 environment `.dockerignore` files use 5 `!` negations.
  - 19 of 26 agent images run `useradd --uid 1000 agent` (python and golang bases). The 7 Node images rename `node`, which is already UID 1000, so E2B's `user` takes 1001 and there is no clash.
  - 11 of 26 agent images run `ln -sfn /opt/deps/node_modules /app/repo/node_modules`.
  - All tasks are single-step. Repos are 0.5-7.3 MB, tests ≤ 0.43 MB.
  - No repo has executable files, symlinks or empty dirs in the fixture. Every `solve.sh` only runs `git apply` or `patch`.
- **tarfile data filter on an absolute symlink (VERIFIED exec, Python 3.13):**
  ```
  ERR AbsoluteLinkError './node_modules' is a link to an absolute path
  ```
  Members after the symlink were not extracted (`out/sub` missing). Python 3.12+ behaves the same; Harbor requires ≥ 3.12.
- **`grade.py` (VERIFIED code):**
  - Imports only the standard library, so it runs under the system `python3` that the E2B login PATH yields.
  - Builds each runner's env from `os.environ["PATH"]` plus `checks.json` `toolchain.env` (`grade.py:1484-1493`), and resolves the runner's python with `shutil.which("python3", path=env["PATH"])` (`:1514-1515`).
  - Python tasks set `toolchain.env.PATH=/opt/deps/venv/bin:…` and are robust.
  - Node and Playwright tasks need only `/usr/local/bin` or `/usr/bin`. The Playwright harness hard-codes `/opt/ms-playwright` and does not need `PLAYWRIGHT_BROWSERS_PATH`.
  - The Go task's `toolchain.env` has `GOCACHE/GOFLAGS/GOMODCACHE/GOPROXY` but **no PATH**, so `go` is not found on E2B.
  - The grader user is UID/GID 2001 (no clash). The privilege drop works as root in the VM.

## 7. Risk register for our packages on E2B (stock `-e e2b` at cfc1bcb)

| # | Severity | Tasks | Docker today | E2B outcome | Fix (author side) |
|---|---|---|---|---|---|
| R1 | **Blocker** | 26 agent, 8 verifier | builds | template build raises before any agent runs: no oracle 1.0, no NOP 0.0 | Make every Dockerfile single-stage. For Node, use one base or copy a vendored, sha256-checked Node tarball from `deps/`. For the CA-cert stage, vendor `ca-certificates.crt` into `deps/` and `COPY` it, or do the first apt over `http://snapshot.debian.org` (apt checks GPG signatures). |
| R2 | **Blocker** (SDK 2.25.0) | 26 | builds | `COPY deps/` → "No files found" | Replace the allow-list `.dockerignore` with a deny-list without `!`, or drop it (the context is only Dockerfile, repo/, deps/, toolchain.json). |
| R3 | **Blocker** | 19 (python, golang agent) | builds | `useradd --uid 1000 agent` → "UID 1000 is not unique" (E2B `user` holds 1000) | Do not pin the UID (`useradd --create-home agent`), or guard it: `getent passwd 1000 >/dev/null && useradd -m agent \|\| useradd -m -u 1000 agent`. |
| R4 | **Blocker** | go-desktop-app (agent + verifier) | builds | `RUN … go mod download` → `go: command not found` (`bash -l` + Debian `/etc/profile`) | In `RUN`, use absolute tool paths (`/usr/local/go/bin/go`) or an inline `export PATH=…&&`. Never rely on image `ENV PATH` during build. |
| R5 | High (if no CLI overrides) | 26 verifiers | 2 vCPU / 4 GB through overrides | verifier template built at the SDK defaults, 2 vCPU / **1024 MB**. Chromium, Postgres and Node tests risk OOM, which looks like a false 0. | Add `cpus = 2` and `memory_mb = 4096` to `[verifier.environment]`. |
| R6 | High | go-desktop-app | passes | runtime `go` not on PATH for the grader, so "could not start runner" | Add `PATH` (and every env var the runner needs that comes from image ENV) to `checks.json` `toolchain.env` for **every** toolchain. |
| R7 | Medium (agent fidelity, not QC) | all | venv on PATH | `00-arena-path.sh` is written by `printf … "$PATH"` during an E2B build, after the login shell reset PATH, so the agent loses `/opt/deps/venv/bin`, `VIRTUAL_ENV` and the `GO*` vars | Write a **literal** PATH from `toolchain.json`, and export the other needed vars in the same profile.d file. Add the same file to verifier images. |
| R8 | Medium (both runtimes, order-dependent) | 11 Node | reported 1.0 (please verify the manifest) | `AbsoluteLinkError` stops `tar` extraction, so a truncated `/app/repo` reaches the verifier | Add `"node_modules"` to `[[artifacts]] exclude` for `/app/repo` (`grade.py` already re-links vendored dirs from `/opt/deps`). |
| R9 | Medium (infra) | all | n/a | no build lock at the pin (concurrent same-alias builds); alias ignores CPU and memory (a stale-size template is reused after an override change); 24 h timeout versus tier max; stream drops on long `exec`; template build happens inside `build_timeout_sec` (1800 s) on the first trial, and the verifier's build only after the agent phase | Pre-warm: one trial per task at `-n 1` before concurrent runs. Use `--force-build` after any resource change. Ask the customer for their tier's `max_length_hours`. |
| R10 | Low | all | modes and symlinks preserved by `docker cp` | the verifier upload flattens symlinks into copies, drops +x and empty dirs; files are owned by E2B's `user` | Design rule: graders call interpreters explicitly (`bash x.sh`, `node x.js`), never rely on +x, symlinks or empty dirs in the uploaded tree. Today's fixtures have none. |
| R11 | Low | all | — | `LANG` unset, soft nofile limit likely 1024, extra tools present (`git`, `curl`, `sudo`, `sshd`), root reachable from `agent` (INFERRED) | `grade.py` already sets `LANG`/`LC_ALL`/`TZ`. Do not treat "agent is non-root" as an integrity guarantee. Grading isolation comes from the separate verifier. |
| R12 | Customer-side | all | — | build-time egress: E2B provisioning needs `deb.debian.org`/`security.debian.org` (Debian bases) and `archive.ubuntu.com` (Playwright noble). Our steps need `snapshot.debian.org`, `registry.npmjs.org`, `pypi.org` + `files.pythonhosted.org`, `proxy.golang.org` + `sum.golang.org`, the Playwright browser CDN, and Docker Hub plus `mcr.microsoft.com` for base pulls. | Ask the customer to confirm the **template-build** allowlist. Consider vendoring wheels, npm tarballs and Go modules into `deps/`. |

**NOP:** none of these makes a NOP score above 0. They turn NOP into an error (R1-R4) or leave it at 0.

**Oracle:** R1-R6 and R8 can turn 1.0 into an error or a 0.

## 8. Q5: recommendations

### 8.1 E2B compatibility checklist (author time; candidates for `shipcheck` detectors)
1. **Single `FROM`**, no `COPY --from` / `ADD --from`, in both `environment/Dockerfile` and `tests/Dockerfile`.
2. **No `!` lines in `.dockerignore`**. SDK 2.25.0 ignores them. Prefer no `.dockerignore`, or a pure deny-list.
3. **No fixed UID that E2B's `user` may already hold** (1000 on images without a UID-1000 account). Do not assume the agent's UID is 1000.
4. **Every `RUN` works in a Debian login shell**: `bash -l -c` with PATH reset to `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`. Use absolute paths for anything outside it. Never write `"$PATH"` into a file during build; write the literal value.
5. **Nothing at runtime depends on Dockerfile or image `ENV`.** Put the full PATH and every needed variable into the agent's `/etc/profile.d/00-arena-*.sh` and the verifier's `checks.json` `toolchain.env`. Add a profile.d file to verifier images as well.
6. **`test.sh` uses an absolute interpreter** (e.g. `/usr/local/bin/python3` or `/usr/bin/python3`), so Docker and E2B choose the same one.
7. **`[verifier.environment]` declares `cpus` and `memory_mb`** (and `build_timeout_sec` if verifier builds are slow).
8. **No absolute symlinks inside artifact sources** (`/app/repo`), or exclude them in `[[artifacts]] exclude` (e.g. `node_modules`).
9. **Graders never depend on file modes, symlinks, empty dirs or ownership** of uploaded artifacts.
10. **Base images:** Debian, Ubuntu, Fedora/RHEL, Arch or Alpine with `/etc/os-release`. Pull by digest (fine). `CMD ["sleep","infinity"]` is fine (2.25.0 joins it to `sleep infinity`). Avoid JSON-array CMDs with quoted arguments.
11. **Images must fit** the disk tier (cloud: 10 or 20 GiB) and the 25 GB base-rootfs limit, and must build within `build_timeout_sec` after E2B's provisioning adds systemd and the other packages (1-3 min, INFERRED).

### 8.2 Validation ladder (cheapest first)

**A. Offline exact-parser check. Do this first; zero cost, VERIFIED to work.** Add Appendix A as a shipcheck detector for both Dockerfiles under **both** e2b 2.25.0 (Harbor's lock) and the current SDK. It catches R1 and R2 exactly. Add simple greps for R3 (`--uid 1000`, `-u 1000`), R4/R7 (`"$PATH"` inside RUN, tools under `/usr/local/go/bin` or `/usr/local/cargo/bin` used in RUN) and R5 (`[verifier.environment]` without `memory_mb`).

**B. Real Harbor@cfc1bcb against E2B cloud. Most faithful without the customer's cluster.** Needs a **Pro** E2B key: Hobby's 1 h cap rejects the hard-coded 24 h sandbox timeout (#1113). On Hobby only, a local patch of `e2b.py:231` to `timeout=3600` works for oracle/NOP runs; do not ship it.
```bash
uv tool install --python 3.12 \
  'harbor[e2b] @ git+https://github.com/harbor-framework/harbor.git@cfc1bcb9480f123b0a702a1428ae3f79ecedb0ca'
# the lock pins e2b==2.25.0; to mirror a pip-installed customer, also test with the latest SDK:
#   uv tool install ... --with 'e2b==2.55.1'
export E2B_API_KEY=e2b_...          # Pro team
T=/abs/path/F2-scenario/a_f2_cli-tool
harbor run -p "$T" -a oracle -e e2b --override-cpus 2 --override-memory-mb 4096 -k 1 -n 1 --force-build -o /tmp/hb-e2b
harbor run -p "$T" -a nop    -e e2b --override-cpus 2 --override-memory-mb 4096 -k 1 -n 1 -o /tmp/hb-e2b
# repeat once WITHOUT the overrides to see what a default-resources customer gets (R5)
# inspect: <trial>/result.json (verifier_result, exception_info), <trial>/artifacts/manifest.json (status of /app/repo),
#          <trial>/verifier/test-stdout.txt, arena-grade.json
```
Keep `-n 1` until each task's two templates exist (no build lock at the pin, R9). Template names are `<task_short_name>__<12-char hash>` and `<…>__<hash of tests/>`.

**C. Local self-hosted E2B. Closest to "self-hosted", heaviest.** It needs a Linux host with `/dev/kvm`: bare metal, a GCP VM with nested virtualization, or an AWS `.metal` instance. It also needs 8-16 GB RAM, `modprobe nbd nbds_max=64` and `vm.nr_hugepages=2048` (`e2b-dev/infra` `DEV-LOCAL.md`). Then `make local-infra`, migrations, `make -C packages/envd build`, seed, run api, orchestrator and client-proxy, and `make -C packages/shared/scripts local-build-base-template`. Point Harbor at it:
```bash
export E2B_API_KEY=e2b_53ae1fed82754c17ad8077fbc8bcdd90 E2B_API_URL=http://localhost:3000 E2B_SANDBOX_URL=http://localhost:3002
harbor run -p "$T" -a oracle -e e2b ...
```
Worth checking: `ls -l /dev/kvm` on a Coder workspace. If KVM is exposed there, this can run on our existing pool. The upstream note says local dev is a "work in progress".

**D. Docker "E2B-sim". Cheap approximation; catches R3-R8 and R10 but not the parser.** Either use a custom Harbor env (`--env ourpkg.e2bsim:E2BSimEnvironment` subclassing `DockerEnvironment`) or a manual run of these steps:
```bash
# 1. image as E2B would build it: add E2B's account first, run RUNs in a Debian login shell
#    (prepend after FROM):   RUN id -u user >/dev/null 2>&1 || useradd --create-home --shell /bin/bash user
#                            SHELL ["/bin/bash", "-l", "-c"]
docker build -t sim-agent  environment/   # with the two lines injected
docker build -t sim-verify tests/         # same injection
# 2. run like E2B: no network, VM-sized, big /dev/shm, every command via env -i + login shell
docker run -d --name a --network none --cpus 2 --memory 4g --memory-swap 4g --shm-size 2g sim-agent sleep infinity
E2BX() { u=$1; shift; docker exec -u "$u" a env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
          HOME="$(docker exec a getent passwd "$u" | cut -d: -f6)" USER="$u" LOGNAME="$u" /bin/bash -l -c "$*"; }
E2BX agent 'command -v python3 node go; echo $PATH'          # R6/R7: compare with Docker
# 3. oracle: copy solution/ as 0644 files, chmod +x as root, run as agent
# 4. collect /app/repo exactly like Harbor: tar czf ... --exclude=... -C /app/repo . as root, then on the host
#    python3 -I -c "import tarfile;tarfile.open('repo.tgz').extractall('out',filter='data')"   # R8
# 5. verifier: container from sim-verify (same flags); empty /app/repo; re-materialise 'out' as REGULAR FILES ONLY,
#    mode 0644, owner uid of 'user' (no symlinks, no empty dirs)                               # R10
#    then: E2BX root 'ARENA_STEP=<step> /tests/test.sh > /logs/verifier/test-stdout.txt 2>&1'; cat /logs/verifier/reward.json
```
Plain `docker run --network none --cpus 2 --memory 4g --cap-drop ALL` is **not** an E2B proxy for privileges: E2B gives root full capabilities in the VM. Keep Docker's default caps; our grader needs CHOWN, SETUID and SETGID, which both have.

### 8.3 Questions to send the customer
1. Do you run the stock `harbor run -e e2b` (`Template.from_dockerfile`), or do you build images with Docker and launch from `docker_image` or a prebuilt template? What is the "Docker compatibility layer" concretely?
2. Which e2b Python SDK version is installed with Harbor (lockfile 2.25.0, or newer)? Which `e2b-dev/infra` version (envd version, kernel)?
3. Do you pass `--override-cpus 2 --override-memory-mb 4096`? Those reach our separate verifier; `task.toml` defaults alone give it 1024 MB.
4. What does your allowlist cover **during template builds** (provisioning `deb.debian.org`/`archive.ubuntu.com`, npm, PyPI, Go proxy, Playwright CDN, Docker Hub, MCR), and at runtime? Do you pass `--allow-environment-host`?
5. What is the tier's `max_length_hours` (Harbor@cfc1bcb requests 24 h) and its `disk_mb`?
6. Could you share the Harbor `result.json` / `exception.txt` for one of our failing trials?

## 9. VERIFIED vs INFERRED summary

**VERIFIED (code, doc or exec)**
- cfc1bcb is on `harbor-miles-v0.20.0`, not main. The E2B fixes after the pin are listed.
- Schema handling in §2.
- The E2B provider: template build from Dockerfile, resource args, no lock, 24 h timeout, `bash -l -c`, root default user, uploads at 0644 owned by the default user without symlinks or empty dirs, tar-based exclude downloads, the separate verifier as a second E2B sandbox built from `tests/`.
- e2b SDK rejects multi-stage (2.25.0 and 2.55.1, executed).
- 2.25.0 ignores `.dockerignore` negation (executed); 2.55.1 handles it (executed).
- SDK appends `USER user`; `ENV` is build-only; default template resources are 2 vCPU / 1024 MB.
- E2B infra: phase order with the `user` account created before our steps, build `RUN` via `bash -l -c`, provisioning packages / root password / sshd config, envd env composition, envd upload semantics.
- Debian `/etc/profile` resets PATH; Debian PAM `nullok` default.
- Guest kernel config values.
- All 26 delivered tasks: multi-stage agents, 8 multi-stage verifiers, 26 negation `.dockerignore`s, 19 `--uid 1000`, 11 absolute `node_modules` symlinks, verifier env without cpus/memory, Go toolchain env without PATH.
- `tarfile` data filter raises on absolute symlinks and stops extraction.

**INFERRED (not executed end to end)**
- E2B's `user` gets UID 1000 on our python and golang bases, so `useradd --uid 1000` fails.
- `go` is missing in E2B build and runtime for the Go task.
- The agent profile.d PATH loses the venv under an E2B build.
- OOM risk at 1024 MB.
- The R8 truncation depends on directory order and can differ between Docker hosts and E2B ext4.
- `/dev/shm` size, nofile limit, `/tmp` wipe effects, `agent`→root via `su`/`ssh`.
- The meaning of the customer's "Docker compatibility layer".
- Self-hosted infra version differences.

---

## Appendix A: offline e2b parser probe (used in §6)

```python
# /tmp/w7r/scripts/e2b_parse_probe.py  — run with: python -I e2b_parse_probe.py <context_dir> [<dockerfile>]
import json, sys, traceback
from e2b import Template
ctx = sys.argv[1]; dockerfile = sys.argv[2] if len(sys.argv) > 2 else f"{ctx}/Dockerfile"
try:
    tpl = Template(file_context_path=ctx).from_dockerfile(dockerfile)   # what Harbor e2b.py:186-190 calls
except Exception as e:
    print(f"PARSE_ERROR {type(e).__name__}: {e}"); sys.exit(0)
try:
    data = json.loads(Template.to_json(tpl))      # computes COPY file hashes with .dockerignore patterns
except Exception as e:
    print(f"SERIALIZE_ERROR {type(e).__name__}: {e}"); sys.exit(0)
print("BASE", data.get("fromImage"), "START", data.get("startCmd"), "READY", data.get("readyCmd"))
for s in data.get("steps", []): print(s.get("type"), s.get("args"))
```

## Appendix B: key source locations

**Harbor @ cfc1bcb**
- `src/harbor/environments/e2b.py`: 102 (workdir), 105-108 (alias), 117-138 (capabilities), 180-214 (template build), 221-236 (sandbox create, `timeout=86_400` at 231), 254-269 (start, no lock), 305-351 (uploads), 359-416 (downloads), 438-507 (exec; root default at 492)
- `src/harbor/environments/base.py`: 223-234 (environment_id), 278-285 (startup env), 392-414 (user resolution), 972-1027 (tar exclusion download: `timeout_sec=120`, `extractall(filter="data")`)
- `src/harbor/trial/trial.py`: 556-625 (separate verifier), 627-666 (verifier env via factory and build timeout), 681-689 (`tests/` as build context), 1091-1095 and 1169-1180 (build timeouts)
- `src/harbor/trial/multi_step.py`: 112-128, 456-466
- `src/harbor/verifier/verifier.py`: 96-103, 191-225
- `src/harbor/trial/artifact_handler.py`: 189-233, 291-326
- `src/harbor/agents/oracle.py`: 96-138
- `src/harbor/models/task/config.py`: 285-327, 334-339, 416-471, 549-605, 634-718, 754-815
- `uv.lock` → `e2b 2.25.0`

**e2b SDK 2.25.0**
- `template/dockerfile_parser.py`: 85-96, 107-141, 193-207, 263-285
- `template/main.py:641`
- `template/utils.py`: 105-161
- `template_async/main.py`: 184-191
- `sandbox_async/commands/command.py`: 252-254
- `connection_config.py`: 211-216
- `envd/versions.py:6`

**e2b-dev/infra @ 74558d3d**
- `packages/orchestrator/pkg/template/build/builder.go`: 410-433
- `…/phases/user/builder.go:57`
- `…/commands/user.go`: 63-65
- `…/commands/env.go`
- `…/sandboxtools/command.go`: 141-157
- `…/phases/base/builder.go`: 186-191
- `…/phases/base/provision.sh`: 94, 100, 136-140
- `…/phases/base/distro/distro.go`: 49-66
- `…/phases/finalize/builder.go`: 183-198
- `…/core/rootfs/files/envd.service.tpl`
- `packages/envd/internal/services/process/handler/handler.go`: 211-275
- `packages/envd/internal/api/upload.go`: 59-111, 263
- `packages/envd/internal/permissions/path.go`: 68-90
- `packages/shared/pkg/id/id.go:17`
- `packages/shared/pkg/featureflags/flags.go`: 699, 970
- `packages/db/migrations/20231220094836_create_triggers_and_policies.sql:5` (self-hosted base tier)
- `DEV-LOCAL.md`

**e2b-dev/fc-kernels @ 82a58ea8**
- `configs/x86_64/6.1.158.config`

**Docs**
- [docs.e2b.dev/template/base-image](https://docs.e2b.dev/template/base-image) ("Multi-stage Dockerfiles are not supported"; supported distros)
- [docs.e2b.dev/template/defining-template](https://docs.e2b.dev/template/defining-template) (build-time-only env)
- [e2b.dev/pricing](https://e2b.dev/pricing) (Hobby 1 h / 10 GiB, Pro 24 h / 20 GiB)
- Harbor `docs/content/docs/tasks/network-policy.mdx` (E2B supports no-network, allowlist, dynamic policy)
- Harbor `docs/content/docs/run-jobs/cloud-sandboxes.mdx` (E2B: no multi-container)
