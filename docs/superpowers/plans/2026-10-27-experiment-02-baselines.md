# Esperimento 02 — piano 2: le baseline sulla VM stack

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `compare` misura FunLess, Spin, tinyFaaS, Serverledge e Fn sulla VM stack. Ciascuna gira sotto lo stesso budget cgroup di nanoFaaS, con la stessa funzione e con la cella, il campionatore, il generatore, le ricevute e il riepilogo del piano 1.

**Architecture:** ogni baseline è un `Platform` del piano 1. La funzione viene da un'unica sorgente, `nanofaas-paper/functions/qr-code`, compilata una volta sola per run sulla VM stack. Le piattaforme si installano una volta per run alle versioni fissate. Ogni cella avvia servizi e funzione dentro `nanolab-sut.slice`:
- i container tramite il `cgroup-parent` di Docker;
- i processi host tramite `systemd-run --slice`.

**Tech Stack:** quello del piano 1, più i wrapper Rust di `nanofaas-paper/functions/qr-code` (core, stdio, funless, spin, fn) e le piattaforme alle versioni della verifica di portabilità del 27 ottobre 2026.

**Spec:** `nanofaas-paper/notes/experiment-02-platform-comparison-prompt.md`; le evidenze di portabilità sono in `nanofaas-paper/notes/evidence.md`, sezione «Workload portability», da cui vengono versioni, digest, contratti e particolarità qui sotto.

## Piani dell'esperimento 02 (aggiornato)

1. Fondazione e nanoFaaS: fatto (`8359e8b..3d89ad2`).
2. **Questo piano:** baseline sulla VM stack.
3. faasd su una VM a parte: budget su containerd senza Docker, terzo ruolo `faasd` nell'ambiente. La valutazione CE scade il 26 dicembre 2026, quindi le misure vanno fatte prima.
4. Pilot e aggregazione: Tabella 3, regione fattibile, scenario congelato.

Sledge è escluso: aWsm non compila la libc WASI precompilata (evidenze, 27 ottobre 2026).

## Vincoli globali

- Una sola implementazione: i wrapper vengono da `nanofaas-paper/functions/qr-code` a un commit registrato nell'identità. Nessuna riscrittura della logica in NanoLab; una piattaforma che non la esegue esce dal confronto.
- Prove solo sulle VM Azure del preset `azure-comparison`; Mac per i soli test unitari.
- Versioni fissate:
  - FunLess `ghcr.io/funlessdev/core@sha256:c75155af88585663d687b478ef0e69e46668e26c3b05d0cab59bc3583fa65954` e `worker@sha256:bb774f588228c704db6a4677e2915de20ce3ae2731ada0b56bd927188f9acd41`, con `postgres:16`;
  - Spin `v4.2.5`, con il checksum del file `checksums-v4.2.5.txt`;
  - tinyFaaS `eff00d060f6f6e96bf8f35475a0e9a8e6e42de32`;
  - Serverledge `45419f50675aa7c8cc3813f626e17f6c28c8a641`, con etcd `gcr.io/etcd-development/etcd:v3.5.13`;
  - Fn `fnproject/fnserver@sha256:16dedb58b2f1be5e02d7a851a66041ea97076801cbdb09cdfee0100296a68686`;
  - builder Rust `rust@sha256:b1d5dd5a22a09404d3e798c0cf051b689077360374eb505fa5bb93c479693d87`, cioè `rust:1.99-alpine`.

  Ogni digest e checksum osservato si registra; una differenza ferma il run.
- Default documentati di ogni piattaforma. Fanno eccezione:
  - la porta d'ingresso, 30080 come nel piano 1;
  - le manopole di concorrenza esistenti, impostate a `fixedConcurrency`: tinyFaaS `threads`, Serverledge `--max_concurrency`;
  - le esclusioni di questa tabella, ciascuna con la ragione scritta nella documentazione.

  | Esclusione | Ragione |
  | --- | --- |
  | Prometheus ed ELK di FunLess | non eseguono funzioni |
  | CoAP e gRPC di tinyFaaS (`coap_PORT=-1`, `grpc_PORT=-1`) | l'ingresso misurato è HTTP |
- Il budget copre la piattaforma intera: per FunLess anche Postgres, per Serverledge anche etcd. Il registry di NanoLab resta fuori, come nel piano 1.
- Nessun token o password di piattaforma in manifest, ricevute, evidenze o commit: il token guest di FunLess si legge sulla VM a ogni cella e passa solo all'ambiente di k6.
- Codice: ruff, basedpyright, import-linter e bandit come nel piano 1. Test rosso, poi verde. Nessuna nuova dipendenza Python.

## Review Focus

1. **Un processo host della piattaforma fuori dallo slice** (Spin, il manager di tinyFaaS, `serverledge`): risorse sottostimate. Test: Task 3 `test_platform_unit_runs_in_the_sut_slice` e Task 7 `test_host_process_outside_the_slice_stops_before_load`.
2. **Un errore mascherato da 200** (tinyFaaS `exit status 1`, Fn `/invoke`): deve risultare risposta errata, mai corretta. Test: Task 2 `test_status_200_with_an_error_body_is_incorrect`.
3. **Artefatto della funzione diverso da quello verificato** (sorgente sporca, commit diverso, hash cambiato): la misura riguarderebbe un'altra funzione. Test: Task 1 `test_dirty_or_moved_workload_source_is_refused` e Task 6 `test_resume_refuses_changed_artifacts`.
4. **Token FunLess finito nelle evidenze**. Test: Task 4 `test_funless_token_never_reaches_evidence`.
5. **Resti di una baseline nella cella seguente** (unità systemd, volumi compose, stato etcd): riposo contaminato. Test: Task 3 `test_teardown_stops_units_and_compose_projects`.

---

### Task 1: Sorgente dei workload e artefatti della funzione

**Files:**
- Create: `packages/nanolab/src/nanolab/tasks/experiment_02/workload.py`
- Modify: `packages/nanolab/src/nanolab/cli/comparison.py` (cattura della sorgente in `capture()` e nell'identità)
- Test: `packages/nanolab/tests/tasks/experiment_02/test_workload.py`

**Interfaces:**
- Consumes: `captured_source_state(path) -> dict` (già in `comparison/prepare.py`); `Vm`, `exec_status`, `comparison_remote_command`, `comparison_transfer_to/from`.
- Produces:
  - `WORKLOADS_ENV = "NANOLAB_WORKLOADS_ROOT"`, la directory `functions/` del repository del paper, obbligatoria quando si sceglie una baseline;
  - `capture_workloads(root: Path) -> dict[str, object]`: commit, stato sporco e sha256 dell'albero di `qr-code`; rifiuta una sorgente sporca;
  - `ARTIFACTS: dict[str, str]`:
    | Chiave | Percorso |
    | --- | --- |
    | `qr-stdio` | `target/x86_64-unknown-linux-musl/release/qr-stdio` |
    | `qr-fn` | `target/x86_64-unknown-linux-musl/release/qr-fn` |
    | `qr-funless` | `target/wasm32-wasip1/release/qr_funless.wasm` |
    | `qr-spin` | `target/wasm32-wasip2/release/qr_spin.wasm` |
  - `build_workloads(stack: Vm, *, archive: Path, remote_root: PurePosixPath, evidence: Path) -> dict[str, str]`: carica la sorgente, compila nel builder Rust fissato e restituisce lo sha256 di ogni artefatto. Prima di restituire esegue `qr-stdio` sul payload del protocollo e richiede lo sha256 del PNG del protocollo quando `pngSha256` è congelato; altrimenti richiede `aa04cabc94ed3cecf89cabd027588b5c6ca19b1d3bdd2efa6d74c3af9b3086c1`, il PNG misurato.

Il builder lancia `cargo build --release --locked` per `-p qr-stdio -p qr-fn` (target `x86_64-unknown-linux-musl`), poi `-p qr-funless --target wasm32-wasip1` e `-p qr-spin --target wasm32-wasip2`, dopo `rustup target add wasm32-wasip1 wasm32-wasip2`. Un `Cargo.lock` mancante o cambiato fa fallire la build.

- [ ] **Step 1: Test**

```python
def test_dirty_or_moved_workload_source_is_refused(tmp_path):
    ...
    # repo git con functions/qr-code: modifica non committata -> ValueError "dirty"


def test_capture_records_commit_and_tree(tmp_path):
    ...
    # due catture della stessa sorgente sono uguali; un nuovo commit le cambia


def test_build_runs_the_pinned_builder_and_checks_the_png(tmp_path):
    ...
    # RecordingProvider: argv con il digest del builder, i quattro -p, --locked;
    # hash del PNG diverso -> ValueError "png"
```

- [ ] **Step 2-4:** rosso, implementazione, verde.
- [ ] **Step 5: Commit** `feat: build the paper's qr-code wrappers for experiment 02`.

### Task 2: Estrattori per le risposte delle baseline

**Files:**
- Modify: `packages/nanolab/src/nanolab/assets/k6/experiment-01.js`, `packages/nanolab/src/nanolab/assets/diagnostics/experiment02_readiness.py`, `packages/nanolab/src/nanolab/tasks/experiment_02/generator.py` (`Extractor`)
- Test: `packages/nanolab/tests/tasks/experiment_02/test_experiment_02_script.py`, `test_readiness_script.py`

**Interfaces:**
- Produces: un nuovo estrattore `json-string-base64`, che legge `field` come stringa JSON da decodificare e poi applica le regole di `json-base64` sul risultato. Serve per il `Result` di Serverledge. Gli altri formati si coprono con quelli esistenti:

  | Piattaforma | Estrattore |
  | --- | --- |
  | FunLess | `json-base64` a `data` |
  | Spin, tinyFaaS, Fn | `json-base64` sull'intero corpo |

- [ ] **Step 1: Test** (stessi casi nello script k6 e nel probe di readiness)

```python
def test_serverledge_result_is_a_json_string_holding_the_png():
    ...
    # {"Success": true, "Result": json.dumps(B64)} -> ok; Result non stringa -> non ok


def test_status_200_with_an_error_body_is_incorrect():
    ...
    # 200 con "exit status 1" (tinyFaaS) e 200 con {"error": ...} (Fn) -> ok false
```

- [ ] **Step 2-4:** rosso, implementazione nei due file con la stessa regola, verde.
- [ ] **Step 5: Commit** `feat: read Serverledge's result in the experiment-02 checks`.

### Task 3: Avvio nel budget e registrazione generica

**Files:**
- Modify: `packages/nanolab/src/nanolab/assets/diagnostics/experiment02_readiness.py`, `packages/nanolab/src/nanolab/tasks/experiment_02/platforms.py`, `packages/nanolab/src/nanolab/tasks/experiment_02/budget.py`
- Test: `test_readiness_script.py`, `test_platforms.py`, `test_budget.py`

**Interfaces:**
- Produces:
  - nella spec di readiness, `register` accetta anche `{"commands": [[argv...], ...]}`, eseguiti in ordine dopo l'API. Ogni comando deve uscire con 0; il tempo dell'ultimo è `registered`, e un fallimento si ripete fino alla scadenza come per l'HTTP;
  - `PLATFORM_UNIT = "nanolab-e02-platform"`; `host_service(argv: Sequence[str], *, env: Mapping[str, str] = {}) -> tuple[str, ...]`, il comando `sudo systemd-run --quiet --collect --unit nanolab-e02-platform --slice nanolab-sut.slice --property=KillMode=control-group [-E k=v] -- argv`;
  - `Platform.host_commands: tuple[str, ...]`: i `comm` dei processi host della piattaforma, che `processes_outside` deve trovare nello slice. È vuoto per nanoFaaS e per il riferimento diretto;
  - la teardown comune ferma `nanolab-e02-platform`, esegue `docker compose -p e02 down -v` se esiste il progetto e rimuove i container. `leftover_containers` resta invariata.

- [ ] **Step 1: Test**

```python
def test_registration_commands_run_after_the_api_and_are_timed(): ...
def test_failed_registration_command_is_retried_until_the_deadline(): ...
def test_platform_unit_runs_in_the_sut_slice():
    ...
    # host_service(("spin", "up")) contiene --slice nanolab-sut.slice e KillMode=control-group


def test_processes_outside_checks_declared_host_commands():
    ...
    # "301 spin /system.slice/x.service" con host_commands=("spin",) -> fuori


def test_teardown_stops_units_and_compose_projects(): ...
```

- [ ] **Step 2-4:** rosso, implementazione, verde.
- [ ] **Step 5: Commit** `feat: start experiment-02 baselines inside the budget`.

### Task 4: FunLess e Spin

**Files:**
- Create: `packages/nanolab/src/nanolab/tasks/experiment_02/baselines/__init__.py`, `funless.py`, `spin.py`, `packages/nanolab/src/nanolab/assets/experiment02/funless-compose.yaml`
- Test: `packages/nanolab/tests/tasks/experiment_02/test_baselines_wasm.py`

**Interfaces:**
- Consumes: Task 1 (`ARTIFACTS`), Task 3 (`register.commands`, `host_service`), `K6Target`.
- Produces:
  - `FunLess(module_path: PurePosixPath)` e `Spin(component_path: PurePosixPath, spin_binary: PurePosixPath)`, che implementano `Platform`;
  - `Platform.secret_headers(vm: Vm) -> Mapping[str, str]`, vuoto per tutte le piattaforme tranne FunLess. La cella lo chiama dopo l'avvio e lo passa a `generator_environment(..., secret_headers=...)`, che lo unisce agli header del target solo nell'ambiente di k6;
  - il probe di readiness riceve gli stessi header in una variabile d'ambiente del comando remoto, mai nella spec.

  Questi header non finiscono in `readiness-spec.json`, nella ricevuta né in altre evidenze.

FunLess:
- compose con il progetto `e02`: `postgres:16`, `init-postgres`, core sulla porta `30080:4000` con un `SECRET_KEY_BASE` casuale per cella e worker. Le immagini sono fissate per digest come nei vincoli globali;
- API `GET /v1/`, che risponde anche 500 quando il servizio è su: accettare qualunque risposta HTTP;
- registrazione: lettura del token guest da `/tmp/funless/tokens` nel container core, `POST /v1/fn` con `{"module":{"name":"e02"}}`, poi multipart `name=qr`, `code=@qr_funless.wasm`;
- target `POST /v1/fn/e02/qr`, corpo `{"args": payload}`, `json-base64` a `data`, `Authorization: Bearer <token>` tra i `secret_headers`.

Spin:
- `host_service(("<spin>", "up", "--listen", "0.0.0.0:30080", "-f", "<spin.toml>"))`;
- API e readiness sulla rotta stessa; nessuna registrazione;
- target `POST /`, corpo il payload, `json-base64` sull'intero corpo; `host_commands = ("spin",)`.

- [ ] **Step 1: Test**

```python
def test_funless_starts_its_compose_and_registers_through_the_api(): ...
def test_funless_token_never_reaches_evidence(tmp_path):
    ...
    # readiness-spec.json, readiness.json e la ricevuta non contengono il token


def test_spin_runs_in_the_platform_unit_on_the_entry_port(): ...
```

- [ ] **Step 2-4:** rosso, implementazione, verde.
- [ ] **Step 5: Commit** `feat: add FunLess and Spin to experiment 02`.

### Task 5: tinyFaaS, Serverledge e Fn

**Files:**
- Create: `packages/nanolab/src/nanolab/tasks/experiment_02/baselines/tinyfaas.py`, `serverledge.py`, `fn.py`
- Test: `packages/nanolab/tests/tasks/experiment_02/test_baselines_containers.py`

**Interfaces:**
- Consumes: Task 1-3.
- Produces: `TinyFaas`, `Serverledge` e `Fn`, che implementano `Platform`.

tinyFaaS:
- manager con `host_service`, `http_PORT=30080`, `coap_PORT=-1`, `grpc_PORT=-1`; `host_commands = ("tinyfaas-linux-amd64", "rproxy")`;
- registrazione: `POST :8080/upload` di `{"name":"qr","env":"binary","threads":<fixedConcurrency>,"zip":…}` da file, con lo zip di `nanofaas-paper/functions/qr-code/tinyfaas/fn.sh` e di `qr-stdio`;
- target `POST /qr`, `json-base64` sull'intero corpo.

Serverledge:
- etcd v3.5.13 come container del progetto (il registry resta fuori);
- `serverledge serverledge-conf.yaml` con `host_service`: `api.port: 30080`, `container.pool.memory` pari a `memoryMiB` del budget, `container.pool.cpus` pari a `cpus`;
- immagine `127.0.0.1:5000/qr-serverledge:<tag>` dal `Dockerfile` di `functions/qr-code/serverledge`, sopra l'Executor costruito a `45419f5`;
- registrazione: `serverledge-cli create --function qr --memory 512 --runtime custom --custom_image … --max_concurrency <fixedConcurrency>`;
- target `POST /invoke/qr`, corpo `{"Params": payload}`, `json-string-base64` a `Result`; `host_commands = ("serverledge",)`.

Fn:
- `fnserver` per digest, privilegiato, con il socket Docker, `iofs` sotto la home e `-p 30080:8080`;
- registrazione: app `e02`, funzione `qr` (immagine `127.0.0.1:5000/qr-fn:<tag>`, 256 MB, timeout uguale a `httpTimeoutSeconds`) e trigger HTTP `/qr`, con i comandi della spec;
- target `POST /t/e02/qr`, `json-base64` sull'intero corpo.

- [ ] **Step 1: Test** (uno per piattaforma su spec di avvio, registrazione e target, più)

```python
def test_concurrency_knobs_follow_the_protocol():
    ...
    # tinyFaaS threads e Serverledge --max_concurrency == fixed_concurrency=3


def test_serverledge_pool_matches_the_budget(): ...
```

- [ ] **Step 2-4:** rosso, implementazione, verde.
- [ ] **Step 5: Commit** `feat: add tinyFaaS, Serverledge and Fn to experiment 02`.

### Task 6: Installazione per run, identità e ripresa

**Files:**
- Create: `packages/nanolab/src/nanolab/tasks/experiment_02/baselines/install.py`
- Modify: `packages/nanolab/src/nanolab/comparison/experiment_02.py` (5 configurazioni: `funless`, `spin`, `tinyfaas`, `serverledge`, `fn`; `platform` diventa `Literal["nanofaas", "direct", "baseline"]`), `packages/nanolab/src/nanolab/cli/comparison.py`, `packages/nanolab/src/nanolab/comparison/manifest.py` (blocco `baselines`)
- Test: `packages/nanolab/tests/tasks/experiment_02/test_baselines_install.py`, `packages/nanolab/tests/cli/test_comparison.py`

**Interfaces:**
- Produces:
  - `install_baselines(stack: Vm, keys: tuple[str, ...], *, artifacts: dict[str, str], evidence: Path) -> dict[str, object]`: installa solo le piattaforme scelte, verifica digest e checksum e restituisce le versioni osservate. È idempotente: ciò che è già installato e coincide non si reinstalla;
  - il blocco `experiment.baselines` dell'identità contiene `workloads` (Task 1), `artifacts` (sha256) e `platforms` (versioni e digest). Il manifest lo richiede quando la selezione contiene una baseline;
  - in ripresa un artefatto o una versione diversi rifiutano la run.
- La CLI esegue `capture_workloads` in `capture()`, `build_workloads` e `install_baselines` dopo la preparazione, una volta per run e prima delle celle. Su un nuovo run il flusso è: slice → prepare → workload → installazione.

- [ ] **Step 1: Test**

```python
def test_install_verifies_spin_checksum_and_image_digests(): ...
def test_install_is_idempotent(): ...
def test_resume_refuses_changed_artifacts(tmp_path): ...
def test_compare_runs_a_baseline_cell_end_to_end(command_case, monkeypatch):
    ...
    # come il test e2e del piano 1, con spin e direct-reference; due celle e ripresa
```

- [ ] **Step 2-4:** rosso, implementazione, verde, suite completa delle aree toccate con `NANOFAAS_ROOT`.
- [ ] **Step 5: Commit** `feat: install experiment-02 baselines once per run`.

### Task 7: Prova su Azure delle cinque baseline

**Files:**
- Modify: `docs/experiment-02.md` (baseline, versioni, esclusioni, particolarità di ciascuna), `packages/nanolab/README.md` se serve
- Test: `packages/nanolab/tests/tasks/experiment_02/test_cell.py` (`test_host_process_outside_the_slice_stops_before_load`)

- [ ] **Step 1: Test** `test_host_process_outside_the_slice_stops_before_load`: una piattaforma con `host_commands=("spin",)` e un `spin` in `/system.slice` produce `ValueError` prima di k6.
- [ ] **Step 2: Controlli di qualità**, come nel Task 10 del piano 1.
- [ ] **Step 3: Prova su Azure:**

```bash
export NANOFAAS_ROOT=$PWD/.nanofaas-source
export NANOLAB_WORKLOADS_ROOT=/Users/micheleciavotta/Documents/nanofaas-paper/functions
./nanolab.sh compare experiment-02-pilot.yaml \
  --environment /Users/micheleciavotta/Downloads/nanolab/packages/nanolab/environments/azure-comparison.yaml \
  --variants funless,spin,tinyfaas,serverledge,fn,direct-reference --repetitions 1 \
  --run-dir runs/experiment-02-baselines-smoke
```

Expected:
- sei ricevute; ogni baseline `measured`, oppure un `startup_failed` documentato;
- il PNG di readiness è `aa04cabc…` per tutte;
- nessun processo della piattaforma fuori dallo slice;
- campionatore fermo dopo ogni cella;
- nessun token nelle evidenze;
- la ripresa della stessa run non esegue nessuna cella e non ricompila.

Se una piattaforma contraddice l'abbinamento P1 (per esempio Fn crea istanze in base al carico), va registrato e segnalato, senza cambiare profilo.

- [ ] **Step 4: Riporta le VM** e il comando di deallocazione, senza eliminarle.
- [ ] **Step 5: Commit** `docs: describe the experiment-02 baselines and their first Azure run`.
