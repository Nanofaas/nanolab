# Esperimento 02 nanoFaaS — piano 1: stack Docker e celle nanoFaaS

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `compare` esegue celle `experiment-02` su una VM stack Azure senza Kubernetes: nanoFaaS P0 e P1 (JVM e native) e il riferimento diretto della funzione, ciascuna dentro un cgroup con budget di CPU e memoria, con il calendario, il ledger, le ricevute e i verdetti dell'esperimento 1 e l'efficienza per risposta utile.

**Architecture:** si riusano provisioning, manifest, prepare/pubblicazione, ricevute e verdetti di `compare`. Una cella non passa da `build_loadtest_plan`, che sul backend container accetta solo ambienti locali: un esecutore dedicato (`tasks/experiment_02/cell.py`) comanda la VM con i comandi remoti già usati da `comparison/prepare.py`. Imposta il budget, riavvia il motore Docker, avvia la piattaforma tramite un adattatore, misura la readiness, lancia k6 sul loadgen, raccoglie eventi e campioni cgroup e scrive la ricevuta. Questo piano implementa gli adattatori nanoFaaS e il riferimento diretto; le baseline arrivano nei piani 2 e 3.

**Tech Stack:** Python 3.12, Pydantic, Sonata (`VmCommandProvider`, `RemoteCommandOperation`), Azure (preset `azure-comparison`), Docker su Ubuntu 24.04 con cgroup v2 e systemd, k6, recipe nanoFaaS/Gradle.

**Spec:** `nanofaas-paper/notes/experiment-02-platform-comparison-prompt.md` (repository privato Nanofaas/nanofaas-paper), con le scelte concordate il 26 ottobre 2026: prove solo su Azure, ambiente unico Docker, niente k3s, faasd su una VM a parte, niente energia.

## Piani dell'esperimento 02

1. **Questo piano:** stack Docker, budget cgroup, campionatore, generatore, celle nanoFaaS e riferimento diretto; prova su Azure con sole celle nanoFaaS.
2. **Workload condiviso e baseline Docker:** tinyFaaS, Fn, Serverledge e faasd (VM a parte); smoke test del prompt (nanoFaaS P1 e una baseline Docker). Richiede la decisione sulla libreria qr-code (vedi Riscontri).
3. **Baseline Wasm:** Spin, Sledge e FunLess; tabella E0 completa.
4. **Pilot e aggregazione:** Tabella 3 con intervalli bootstrap, dati della regione fattibile, proposta dello scenario congelato.

## Riscontri nel checkout (main `8359e8b`, 26 ottobre 2026)

- `cli/comparison.py:434-443` accetta solo loadtest con backend `k8s`; `plans/loadtest.py:234` rifiuta il backend `container` fuori da un ambiente locale. Le celle Docker su Azure non possono passare da `build_loadtest_plan`.
- `cli/provisioning.py:72-82` installa k3s per ogni loadtest non containerd. `provision-base.yml` installa `docker.io`, `docker-buildx` e `openjdk-25-jdk-headless`; `ensure-registry.yml` avvia il registry `nanofaas-e2e-registry` sulla porta 5000.
- `comparison/target.py:59-95` legge l'identità del cluster k3s: senza k3s fallisce.
- `comparison/experiment_01.py` ha ledger, verdetti e ricevute riusabili, ma `write_cell_result` richiede lo snapshot Prometheus e scrive `cell.variant.key`; `summarize_phases` legge le serie `container_memory_bytes@control-plane` e `container_cpu_cores_max@control-plane`.
- `comparison/manifest.py`: `_Identity.complete_identity` richiede una recipe per ogni variante e `new_comparison_manifest` legge `cell.variant`; entrambi vanno adattati alle configurazioni senza recipe. `_resource` in `comparison/experiment_01.py` riassume i punti disponibili senza verificare la copertura: il riepilogo experiment-02 deve applicare il controllo del Task 5.
- `plans/runtime_comparison.py:300-309`: cAdvisor scartato sotto Docker Desktop; `one_shot/vm_sampler.py` legge già `memory.current` e `cpu.stat` di cgroup v2.
- nanoFaaS al pin CI `3327c4aa` (contiene #256, `maxHeap`):
  - il provider container usa `/var/run/docker.sock` e le variabili di `deploy/compose/compose.yaml`;
  - `FunctionSpec` ha `executionMode`, `endpointUrl`, `concurrency`, `queueSize` e `maxRetries` (`docs/function-definition.md`);
  - il runtime Rust serve `/invoke` con corpo `{"input", "metadata", "headers"}` e fa callback solo se è configurato un URL.
- La logica della qr-code Rust è in `functions/rust/qr-code/src/main.rs`, non in una libreria. Il piano 2 deve riusarla dalle baseline: serve una modifica in nanoFaaS, da chiedere all'autore come prescrive la spec.
- Sul Mac la suite completa ha 391 fallimenti e 45 errori in `soak/`, `one_shot/`, `tasks/`, `heap_analysis/` e `plans/`, dovuti a funzioni solo Linux (pidfd e procfs). Le aree toccate (`comparison/`, `config/`, `cli/test_comparison.py`, `plans/test_runtime_comparison.py`) danno 471 passati e 20 saltati.

## Vincoli globali

- Prove solo su VM Azure remote con il preset `azure-comparison` (stack `Standard_D8s_v5` amd64, loadgen `Standard_D2s_v5`): Multipass o Docker locale non valgono come evidenza.
- Ambiente di esecuzione unico Docker; k3s escluso, anche come riga di sola nanoFaaS.
- Il budget copre il deployment completo in un cgroup v2 dedicato, con quota CPU, `memory.max` e swap a 0. Il motore Docker (dockerd e containerd) si riporta anche come voce separata; generatore e agenti di misura restano fuori.
- Memoria = working set del cgroup (`memory.current` meno `inactive_file`); CPU dal tempo CPU cumulativo, in core su finestre di 30 s, con serie a 5 s.
- Si registra separatamente il costo CPU/memoria degli agenti sulla VM stack, fuori dal budget e senza includere il registry. Per nanoFaaS si conservano le serie dello scheduler; una metrica assente non diventa zero.
- Per ogni coorte, l'efficienza integra il deployment su `[inizio degli arrivi, fine degli arrivi + drainSeconds]`. Le finestre possono sovrapporsi e non si sommano; la copertura completa è richiesta anche per medie e massimi delle risorse.
- Stessa revisione nanoFaaS dell'esperimento 1 (pin CI `3327c4aa`): Serial GC, heap massimo 64 MiB, JIT completo e native -O3, `build-metadata` in ogni build.
- Nessuna compilazione durante le misure; ogni cella riparte da un deployment pulito, con il motore Docker riavviato.
- Avvii falliti, OOM e piattaforme non disponibili sono risultati, mai valori di prestazione. Con N_ok,L = 0 il costo per risposta è indefinito; una misura mancante resta distinta da zero.
- Niente energia.
- Codice: ruff a 88 colonne, basedpyright, import-linter; test puri senza `NANOFAAS_ROOT`; il checkout nanoFaaS dell'operatore resta in sola lettura (si usa `.nanofaas-source` del worktree).
- La campagna non parte prima che l'autore approvi lo scenario congelato.

## Review Focus

1. **Processo del deployment fuori dal cgroup** (uno shim o un container con un parent diverso): memoria e CPU sottostimate. La cella deve fermarsi prima del carico elencando i processi esterni. Test: Task 4 `test_processes_outside_the_slice_are_reported`, Task 8 `test_process_outside_the_slice_stops_before_load`.
2. **Container avanzati dalla cella precedente**, ripartiti al riavvio del motore per la policy `unless-stopped`: riposo contaminato. Prima del deploy restano solo il registry e nient'altro. Test: Task 8 `test_leftover_containers_are_removed_before_deploy`.
3. **Budget non applicato** (slice assente o percorso diverso): misura senza limiti. Si rilegge `cpu.max`, `memory.max` e `memory.swap.max` e si rifiuta ogni differenza. Test: Task 4 `test_parse_budget_refuses_an_unbounded_slice`, Task 8 `test_budget_mismatch_stops_before_deploy`.
4. **Campionatore morto a metà o rimasto attivo dopo un errore**: buchi e celle contaminate. Un buco oltre due intervalli rende ignota la finestra anche per medie e massimi; cleanup e recupero delle evidenze avvengono su ogni uscita. Test: Task 5 `test_gap_makes_the_window_unknown_not_zero`, Task 8 `test_next_cell_can_start_after_readiness_timeout`, Task 9 `test_gap_invalidates_resource_means_and_maxima`.
5. **OOM della funzione o del motore, prima o durante il carico**: esito errato o perso. Un incremento documentato di `oom_kill` nel tentativo, oppure un container del tentativo con `OOMKilled`, prevale sul timeout di readiness e sugli errori di esecuzione. Test: Task 8 `test_oom_before_readiness_is_oom`, `test_engine_oom_during_restart_is_oom`, `test_oom_kill_during_load_is_oom`.

---

### Task 1: Protocollo, scenario e preset pilot

**Files:**
- Create: `packages/nanolab/src/nanolab/config/experiment_02.py`
- Modify: `packages/nanolab/src/nanolab/config/scenario.py` (`LoadProfile`, campo `experiment_02`, validatore vicino a quello di experiment-01, righe 540-583)
- Create: `packages/nanolab/src/nanolab/assets/presets/scenarios/experiment-02-pilot.yaml`
- Test: `packages/nanolab/tests/config/test_experiment_02.py`

**Interfaces:**
- Produces: `Budget` (Pydantic, strict, alias `cpus`, `memoryMiB`) con `label -> str` uguale a `f"cpu{cpus:g}-mem{memory_mib}"`; `Experiment02Protocol(Experiment01Protocol)` con `budgets: list[Budget]` (almeno 1), `reference_budget: Budget` (alias `referenceBudget`), `readiness_timeout_seconds: PositiveInt` (alias `readinessTimeoutSeconds`), `png_sha256: str | None` (alias `pngSha256`, 64 esadecimali), `order_seed: int` (alias `orderSeed`, almeno 0); `EXPERIMENT_02_FUNCTION = "qr-code-rust"`; `ScenarioConfig.experiment_02: Experiment02Protocol | None` (alias `experiment02`).

- [ ] **Step 1: Scrivi i test**

```python
def test_budget_label():
    assert Budget(cpus=1.5, memoryMiB=1024).label == "cpu1.5-mem1024"


def test_reference_budget_must_belong_to_the_grid():
    with pytest.raises(ValidationError, match="referenceBudget"):
        protocol(
            budgets=[{"cpus": 2, "memoryMiB": 2048}],
            referenceBudget={"cpus": 1, "memoryMiB": 1024},
        )


def test_duplicate_budgets_are_rejected():
    with pytest.raises(ValidationError, match="unique"):
        protocol(budgets=[{"cpus": 2, "memoryMiB": 2048}] * 2)


def test_frozen_protocol_requires_png_hash():
    with pytest.raises(ValidationError, match="pngSha256"):
        protocol(status="frozen", generator=GENERATOR, pngSha256=None)


def test_experiment_02_is_a_container_loadtest_of_qr_code_alone():
    with pytest.raises(ValidationError, match="container loadtest"):
        scenario(backend="k8s")
    with pytest.raises(ValidationError, match="experiment02 block"):
        scenario(experiment02=None)


def test_pilot_preset_loads():
    config = load_preset("experiment-02-pilot.yaml")
    assert config.experiment_02.reference_budget in config.experiment_02.budgets
```

`protocol(...)` e `scenario(...)` sono helper locali che partono dai valori del preset pilot.

- [ ] **Step 2: Esegui i test e verifica che falliscano**

Run: `uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/config/test_experiment_02.py -q`
Expected: FAIL, `ModuleNotFoundError: nanolab.config.experiment_02`.

- [ ] **Step 3: Implementa protocollo, campo dello scenario e preset**

Validatore dello scenario: `loadProfile: experiment-02` se e solo se c'è il blocco; richiede `workflow: loadtest`, `backend: container`, `functions == ["qr-code-rust"]`, nessun blocco `experiment01` e nessuna opzione di carico legacy (stesse escluse di experiment-01). Preset pilot: calendario, criteri, `fixedConcurrency: 2`, `queueCapacity: 200` e payload `experiment-01-qr-1024.json` copiati da `experiment-01-pilot.yaml`; `budgets: [{cpus: 2, memoryMiB: 2048}]`, budget di riferimento uguale, `readinessTimeoutSeconds: 300`, `pngSha256: null`, `orderSeed: 20261026`, `status: pilot`. Commento in testa in italiano, come il preset dell'esperimento 1.

- [ ] **Step 4: Esegui i test e verifica che passino**

Run: lo stesso comando, più `packages/nanolab/tests/config`.
Expected: PASS, nessuna regressione nei test di configurazione esistenti.

- [ ] **Step 5: Commit**

```bash
git add packages/nanolab/src/nanolab/config packages/nanolab/src/nanolab/assets/presets/scenarios/experiment-02-pilot.yaml packages/nanolab/tests/config/test_experiment_02.py
git commit -m "feat: add the experiment-02 protocol and pilot scenario"
```

### Task 2: Configurazioni, celle, matrice, identità e ricevute

**Files:**
- Create: `packages/nanolab/src/nanolab/comparison/experiment_02.py`
- Modify: `packages/nanolab/src/nanolab/comparison/matrix.py` (proprietà `ComparisonCell.key`; annotazioni del percorso `pending` per accettare anche `Experiment02Cell`)
- Modify: `packages/nanolab/src/nanolab/comparison/experiment_01.py` (`write_cell_result` e `read_cell_result` usano `cell.key`; parametro `required`)
- Modify: `packages/nanolab/src/nanolab/comparison/manifest.py` (`_complete_experiment`, `_Identity.complete_identity`, `new_comparison_manifest` e annotazioni dei relativi chiamanti)
- Test: `packages/nanolab/tests/comparison/test_experiment_02.py`

**Interfaces:**
- Consumes: `Budget`, `Experiment02Protocol` (Task 1).
- Produces:
  - `Configuration(key: str, label: str, platform: Literal["nanofaas", "direct"], profile: Literal["P0", "P1"] | None, runtime: Literal["jvm", "native"] | None, recipe: str | None)`, frozen;
  - `EXPERIMENT_02_CONFIGURATIONS`, in quest'ordine: `nanofaas-p1-jvm` (pubblica la funzione), `nanofaas-p1-native`, `nanofaas-p0-jvm`, `nanofaas-p0-native`, `direct-reference`; `CONFIGURATIONS_BY_KEY`;
  - `Experiment02Cell(configuration, budget, repetition)`, frozen, con `key`, `label = f"{key} {budget.label} run {repetition}"` e `run_dir(root) = root / key / budget.label / f"run-{repetition}"`;
  - `EXPERIMENT_02_ORDER_POLICY = "experiment-02-seeded-shuffle-v1"`;
  - `build_experiment_02_matrix(configurations, budgets, repetitions, *, seed: int) -> tuple[Experiment02Cell, ...]`;
  - `experiment_02_identity(protocol, cells, *, payload: bytes, script: Path, selected: tuple[str, ...], seed: int) -> dict[str, object]`, con i campi `profile`, `orderPolicy`, `seed`, `order`, `protocol`, `protocolSha256`, `payload`, `script`, `configurations`, `budgets`, `subset`;
  - `SAMPLES = "metrics/cgroup-samples.jsonl"`, `SCHEDULER = "metrics/scheduler-samples.jsonl"` ed `EXPERIMENT_02_EVIDENCE = (SUMMARY, EVENTS, SAMPLES)`; alle celle nanoFaaS si richiede anche `(SCHEDULER,)`, mentre il riferimento diretto non produce questo file. Solo il report diretto dichiara `schedulerCoverage: not_applicable`;
  - `write_cell_result(..., required: tuple[str, ...] = (SUMMARY, EVENTS, SNAPSHOT))`; la ricevuta mantiene la chiave `"variant"` con valore `cell.key`. `required` governa sia la verifica per `measured` sia `notProduced`; gli esiti falliti richiedono diagnostiche, non artefatti inventati. Aggiornare le annotazioni di ricevute e `experiment_completed` per entrambe le classi di cella.

Nel manifest `variants` indica le configurazioni misurate, mentre `profiles` e `publications` indicano le build: per experiment-02 sono le configurazioni selezionate con recipe, più `nanofaas-p1-jvm` come produttore condiviso della funzione. `_Identity.complete_identity` valida queste build anche quando il produttore non è una cella selezionata; non richiede una recipe per `direct-reference`. `_complete_experiment` valida i campi specifici del profilo e l'ordine completo delle triple configurazione × budget × ripetizione, senza duplicati o omissioni, di lunghezza `len(variants) * len(budgets) * repetitions`.

`new_comparison_manifest` tratta esplicitamente `Experiment02Cell`: usa `cell.configuration` e descrive il riferimento diretto senza attribuirgli un'immagine control plane; la sua immagine funzione viene dalla distribuzione del produttore condiviso. Conserva invariato il ramo storico/experiment-01. Creazione, serializzazione, rilettura, confronto degli input e resume devono funzionare con la nuova identità.

Le annotazioni fra `matrix`, `experiment_01`, `experiment_02` e `manifest` usano import sotto `TYPE_CHECKING` quando servono soltanto ai tipi, evitando dipendenze circolari a runtime.

- [ ] **Step 1: Scrivi i test**

```python
def test_matrix_runs_every_pair_once_per_repetition():
    cells = build_experiment_02_matrix(CONFIGS[:2], BUDGETS[:2], 3, seed=7)
    assert len(cells) == 12
    for rep in (1, 2, 3):
        pairs = {(c.key, c.budget.label) for c in cells if c.repetition == rep}
        assert len(pairs) == 4


def test_matrix_order_is_reproducible_and_reshuffled_per_repetition():
    a = build_experiment_02_matrix(CONFIGS, BUDGETS, 2, seed=7)
    assert a == build_experiment_02_matrix(CONFIGS, BUDGETS, 2, seed=7)
    first = [c.label.rsplit(" run", 1)[0] for c in a if c.repetition == 1]
    second = [c.label.rsplit(" run", 1)[0] for c in a if c.repetition == 2]
    assert first != second


def test_cell_directory_and_label(tmp_path):
    cell = Experiment02Cell(CONFIGURATIONS_BY_KEY["nanofaas-p1-jvm"], BUDGET_2_2048, 1)
    assert (
        cell.run_dir(tmp_path)
        == tmp_path / "nanofaas-p1-jvm" / "cpu2-mem2048" / "run-1"
    )
    assert cell.label == "nanofaas-p1-jvm cpu2-mem2048 run 1"


def test_receipt_round_trip_with_cgroup_samples(tmp_path):
    ...
    # parametrizza diretto (tre file) e nanoFaaS (quattro file), required coerente;
    # read_cell_result restituisce la ricevuta; modificando i campioni -> ValueError


def test_scheduler_evidence_is_required_only_for_nanofaas(tmp_path):
    ...
    # senza SCHEDULER: measured rifiutato per P1; diretto valido e senza
    # SCHEDULER in notProduced, perché non applicabile


def test_manifest_accepts_an_experiment_02_identity(): ...
def test_manifest_rejects_an_order_of_the_wrong_length(): ...
def test_manifest_round_trip_with_direct_reference_and_two_budgets(tmp_path):
    ...
    # P1 + diretto, due budget, una ripetizione: quattro celle; nessuna recipe
    # per il diretto; new/write/read_comparison_manifest conserva tutta l'identità


def test_direct_only_manifest_requires_the_shared_function_publisher():
    ...
    # variants=[direct-reference], profiles=[nanofaas-p1-jvm]; rimuovere il
    # produttore deve fallire, senza aggiungerlo alle celle misurate


def test_manifest_rejects_duplicate_configuration_budget_repetition(): ...
def test_resume_skips_only_the_receipted_budget(tmp_path):
    ...
    # stessa configurazione, due budget: una ricevuta salta soltanto quella cella
```

- [ ] **Step 2: Esegui i test e verifica che falliscano** — `pytest ... tests/comparison/test_experiment_02.py -q`, FAIL per import mancante.

- [ ] **Step 3: Implementa**

Ordine: per ogni ripetizione, tutte le coppie (configurazione, budget) nell'ordine dichiarato, mescolate con `random.Random(f"{seed}:{repetition}")`. Il seed con stringa è deterministico fra esecuzioni; `compare` passa `seed=protocol.order_seed`, così l'ordine fa parte dell'identità dello scenario.

- [ ] **Step 4: Esegui i test e verifica che passino** — più tutta `tests/comparison` e `tests/cli/test_comparison.py`: le ricevute dell'esperimento 1 non cambiano.

- [ ] **Step 5: Commit** — `feat: add experiment-02 cells, matrix and identity`.

### Task 3: Recipe container P0/P1 e contratto di prepare

**Files:**
- Create: `assets/presets/recipes/experiment-02-p1-jvm.yaml`, `experiment-02-p1-native.yaml`, `experiment-02-p0-jvm.yaml`, `experiment-02-p0-native.yaml`
- Modify: `assets/presets/recipes/README.md` (sezione "Experiment 02 profiles")
- Modify: `packages/nanolab/src/nanolab/comparison/profiles.py`
- Test: `packages/nanolab/tests/comparison/test_experiment_02_profiles.py`

**Interfaces:**
- Consumes: `Configuration`, `CONFIGURATIONS_BY_KEY` (Task 2).
- Produces:
  - `EXPERIMENT_02_FUNCTIONS = {"qr-code-rust": ("qr-code", "rust")}`;
  - `EXPERIMENT_02_MODULES = {"P0": ("build-metadata",), "P1": ("container-deployment-provider", "async-queue", "build-metadata")}`;
  - `experiment_02_profiles(tool_root: Path, keys: tuple[str, ...]) -> dict[str, Path]`, con `nanofaas-p1-jvm` sempre primo, perché pubblica la funzione usata anche dal riferimento diretto; `direct-reference` non ha recipe;
  - `experiment_02_options(profile: Path, configuration: Configuration, *, shares: bool) -> dict[str, object]`;
  - `ProfileContract` con un campo `kind: Literal["historical", "experiment-01", "experiment-02"]` e `EXPERIMENT_02_CONTRACT`; `contract_for(identity)` sceglie in base a `identity["experiment"]["profile"]`.

Contenuto delle recipe: registry `127.0.0.1:5000/nanofaas`, tag `experiment-02`, immagini `control-plane-e02-<p0|p1>-<jvm|native>`. JVM: `args: [-XX:+UseSerialGC, -Xmx64m]`. Native: `builder: host`, `native: {gc: serial, maxHeap: 64m}`. Solo `p1-jvm` dichiara `functions: [{name: qr-code, sdk: rust, container: {image: rust-qr-code}}]`.

- [ ] **Step 1: Scrivi i test**

```python
def test_profiles_put_the_function_publisher_first(tool_root):
    keys = experiment_02_profiles(tool_root, ("nanofaas-p0-native", "direct-reference"))
    assert list(keys) == ["nanofaas-p1-jvm", "nanofaas-p0-native"]


def test_p0_has_neither_provider_nor_queue(tool_root):
    options = experiment_02_options(
        tool_root / "recipes/experiment-02-p0-jvm.yaml",
        CONFIGURATIONS_BY_KEY["nanofaas-p0-jvm"],
        shares=False,
    )
    assert options["modules"] == ["build-metadata"]


def test_options_reject_a_recipe_with_the_wrong_modules(tmp_path): ...
def test_contract_for_an_experiment_02_identity_is_the_experiment_02_contract(): ...
```

- [ ] **Step 2: Esegui i test e verifica che falliscano.**
- [ ] **Step 3: Implementa recipe, opzioni e contratto.**
- [ ] **Step 4: Esegui i test e verifica che passino.**
- [ ] **Step 5: Valida le recipe con Gradle, come la CI**

Run (dal worktree): `cd .nanofaas-source && for r in ../packages/nanolab/src/nanolab/assets/presets/recipes/experiment-02-*.yaml; do ./gradlew -q validateRecipe -Precipe="$PWD/$r" || echo "INVALID $r"; done`
Expected: nessun `INVALID`. Se P0 (solo `build-metadata`) viene rifiutata, fermati e riporta l'errore: P0 sarebbe non disponibile, e la decisione spetta all'autore.

- [ ] **Step 6: Commit** — `feat: add experiment-02 container recipes and contract`.

### Task 4: Budget cgroup e controllo del motore Docker

**Files:**
- Create: `packages/nanolab/src/nanolab/tasks/experiment_02/__init__.py`, `budget.py`
- Test: `packages/nanolab/tests/tasks/experiment_02/test_budget.py`

**Interfaces:**
- Consumes: `Budget` (Task 1).
- Produces:
  - `SUT_SLICE = "nanolab-sut.slice"` e `INFRA_SLICE = "nanolab-infra.slice"`, figli di `nanolab.slice`;
  - `SUT_CGROUP = PurePosixPath("/sys/fs/cgroup/nanolab.slice/nanolab-sut.slice")`;
  - `OBSERVER_SLICE = "nanolab-infra-observers.slice"` e `OBSERVER_CGROUP = PurePosixPath("/sys/fs/cgroup/nanolab.slice/nanolab-infra.slice/nanolab-infra-observers.slice")`, dedicati agli agenti di misura, senza registry;
  - `ENGINE_CGROUPS = ("docker.service", "containerd.service")`;
  - `REGISTRY_CONTAINER = "nanofaas-e2e-registry"`;
  - `slice_setup_operation() -> RemoteCommandOperation` (id `experiment02.slices`);
  - `apply_budget_argv(budget: Budget) -> tuple[str, ...]`, `restart_engine_argv() -> tuple[str, ...]`, `read_budget_argv() -> tuple[str, ...]`;
  - `parse_budget(text: str) -> Budget`;
  - `process_listing_argv() -> tuple[str, ...]` e `processes_outside(listing: str) -> list[str]`.

Lo script di `slice_setup_operation` è idempotente e fa, nell'ordine:
1. scrive le unità `nanolab.slice`, `nanolab-sut.slice`, `nanolab-infra.slice` e `nanolab-infra-observers.slice`;
2. aggiunge i drop-in `Slice=nanolab-sut.slice` per `docker.service` e `containerd.service`;
3. unisce `"cgroup-parent": "nanolab-sut.slice"` in `/etc/docker/daemon.json`, con `python3 -c`, senza perdere le chiavi esistenti;
4. esegue `swapoff -a`, `daemon-reload` e il riavvio di containerd e docker;
5. ricrea il registry con `--cgroup-parent=nanolab-infra.slice --restart unless-stopped -p 5000:5000 registry:2`, così non finisce nel budget.

- [ ] **Step 1: Scrivi i test**

```python
def test_apply_budget_argv():
    assert apply_budget_argv(Budget(cpus=1.5, memoryMiB=1024)) == (
        "sudo",
        "systemctl",
        "set-property",
        "--runtime",
        "nanolab-sut.slice",
        "CPUQuota=150%",
        "MemoryMax=1024M",
        "MemorySwapMax=0",
    )


def test_parse_budget_reads_back_the_cgroup():
    assert parse_budget("150000 100000\n1073741824\n0\n") == Budget(
        cpus=1.5, memoryMiB=1024
    )


def test_parse_budget_refuses_an_unbounded_slice():
    with pytest.raises(ValueError, match="unbounded"):
        parse_budget("max 100000\n1073741824\n0\n")


def test_slice_setup_moves_the_engine_into_the_budget_and_the_registry_out():
    script = slice_setup_operation().argv[-1]
    assert "Slice=nanolab-sut.slice" in script
    assert '"cgroup-parent": "nanolab-sut.slice"' in script
    assert "--cgroup-parent=nanolab-infra.slice" in script


def test_observers_have_a_separate_slice_from_the_registry():
    ...
    # OBSERVER_CGROUP è fuori da SUT_CGROUP; registry nel parent infra,
    # campionatore e raccolta scheduler soltanto nello slice observers


def test_processes_outside_the_slice_are_reported():
    listing = (
        "101 containerd-shim /system.slice/containerd.service\n"
        "102 dockerd /nanolab.slice/nanolab-sut.slice/docker.service\n"
        "103 registry /nanolab.slice/nanolab-infra.slice/docker-ab.scope\n"
    )
    assert processes_outside(listing) == [
        "101 containerd-shim /system.slice/containerd.service"
    ]
```

- [ ] **Step 2-4:** test che falliscono, implementazione, test che passano.
- [ ] **Step 5: Commit** — `feat: add the experiment-02 cgroup budget`.

### Task 5: Campionatore cgroup, costo degli agenti e serie dello scheduler

**Files:**
- Create: `packages/nanolab/src/nanolab/assets/diagnostics/experiment02_cgroup_sampler.py` (sulla VM; solo libreria standard)
- Create: `packages/nanolab/src/nanolab/tasks/experiment_02/samples.py`
- Test: `packages/nanolab/tests/tasks/experiment_02/test_samples.py`

**Interfaces:**
- Consumes: `SUT_CGROUP`, `ENGINE_CGROUPS`, `OBSERVER_CGROUP` (Task 4), `SAMPLES` e `SCHEDULER` (Task 2).
- Produces:
  - lo script sulla VM, `--root DIR --observer-root DIR --interval S --out FILE [--metrics-url URL --scheduler-out FILE] [--once]`: ogni intervallo scrive una riga JSON `{"ts": float, "cgroups": {rel: {"memoryCurrent", "inactiveFile", "cpuUsageUsec", "nrThrottled", "throttledUsec", "oomKill"}}}` per la radice `"."` e i figli fino alla profondità 2; legge anche `OBSERVER_CGROUP` sotto la chiave `"@observers"`. Le due opzioni scheduler devono essere presenti insieme oppure entrambe assenti. Registra errori di lettura e campi mancanti senza sostituirli con zero. Scrive un primo campione prima di segnalarsi pronto e un ultimo campione su SIGTERM, poi chiude i file aperti;
  - `read_cgroup_samples(path: Path, *, interval_seconds: float = 5.0) -> CgroupSamples` (`samples: list[dict]`, `malformed: int`, `interval_seconds: float`);
  - `window_covered(samples, start, end, *, cgroup=".", fields: tuple[str, ...]) -> bool`: richiede campioni validi che coprano entrambi gli estremi, timestamp crescenti, nessun reset dei contatori richiesti e nessun intervallo oltre `2 * interval_seconds`; l'intervallo atteso non si deduce dai campioni superstiti;
  - `working_set(samples, cgroup=".") -> list[tuple[float, float]]`;
  - `cpu_cores(samples, cgroup=".", window_seconds=30.0) -> list[tuple[float, float]]`, con un valore solo se è coperta tutta la finestra retrospettiva di 30 s;
  - `cpu_seconds(samples, start, end, cgroup=".") -> float | None` e `memory_byte_seconds(samples, start, end, cgroup=".") -> float | None`: usano `window_covered`; interpolano linearmente gli estremi e integrano la memoria con i trapezi. Copertura mancante o contatore resettato rende ignota la misura;
  - `oom_kills(samples, cgroup=".") -> int | None`: incremento del contatore nel tentativo, non valore assoluto ereditato dalla cella precedente; con estremi mancanti o reset non documentato restituisce `None`;
  - `container_count(samples) -> list[tuple[float, int]]`: quanti cgroup figli `docker-*.scope` esistono a ogni campione, cioè le istanze vive.

Il Task 9 usa direttamente questi campioni e le serie numeriche derivate; nessuna conversione nel formato snapshot Prometheus. Il motore somma solo i due cgroup disgiunti dei servizi; gli observer non entrano nel totale deployment.

Per nanoFaaS lo stesso processo raccoglie ogni 5 s l'esposizione Prometheus completa da `http://127.0.0.1:8081/actuator/prometheus`, usando `urllib.request` con timeout di 1 s, e salva in `SCHEDULER` righe `{ts, status, body, error}`. Conserva testo, etichette, contatori e bucket originali: così mantiene anche le metriche usate da `core_queries` e dalle query dei moduli in `metrics/catalogue.py`, senza introdurre un server Prometheus. Un errore HTTP produce `status: error` senza saltare il campione cgroup. Senza le opzioni scheduler non esegue richieste HTTP e non crea il file, come per il riferimento diretto. Una serie non esposta da P0 resta assente; non si inventano metriche di coda. La raccolta parte prima del deploy e termina prima del teardown; gli errori attesi prima della readiness restano nelle evidenze ma la copertura di carico si valuta dal calendario k6.

`@observers` include il campionatore e la raccolta HTTP nello stesso processo. Si salvano unità, PID e percorso cgroup degli agenti; eventuali altri agenti persistenti sulla stack devono stare nello stesso slice. Il registry resta fuori da questa voce. Il riepilogo del Task 9 applica `window_covered` per ogni risorsa anche a medie, massimi e conteggi, non soltanto agli integrali; una risorsa ignota non annulla il ledger valido.

- [ ] **Step 1: Scrivi i test**

```python
def test_sampler_reads_a_cgroup_tree_once(tmp_path):
    root = fake_cgroup(tmp_path, memory_current=1000, inactive_file=200, usage_usec=5)
    out = tmp_path / "s.jsonl"
    observer = fake_cgroup(
        tmp_path / "observer", memory_current=100, inactive_file=20, usage_usec=1
    )
    subprocess.run(
        [
            sys.executable,
            "-I",
            SCRIPT,
            "--root",
            str(root),
            "--observer-root",
            str(observer),
            "--interval",
            "5",
            "--once",
            "--out",
            str(out),
        ],
        check=True,
    )
    line = json.loads(out.read_text())
    assert line["cgroups"]["."]["memoryCurrent"] == 1000


def test_working_set_subtracts_inactive_file(): ...  # 1000 - 200 == 800
def test_cpu_cores_over_a_thirty_second_window():
    samples = series_at(
        list(range(0, 31, 5)), usage_usec=[t * 500_000 for t in range(0, 31, 5)]
    )
    assert cpu_cores(samples)[-1] == (30.0, 0.5)


def test_cpu_seconds_and_memory_byte_seconds_over_a_window(): ...
def test_gap_makes_the_window_unknown_not_zero():
    samples = series_at([0, 5, 30], usage_usec=[0, 1, 2])  # buco di 25 s
    assert cpu_seconds(samples, 0, 30) is None


def test_truncated_last_line_is_counted_not_parsed(): ...
def test_oom_kill_count(): ...
def test_oom_count_uses_attempt_delta_not_previous_cell_total():
    ...
    # contatore 3 -> 3: 0 OOM; 3 -> 4: 1; estremi mancanti/reset: None


def test_gap_invalidates_a_cpu_rate_window():
    ...
    # un buco 5 -> 30 s impedisce di emettere il valore CPU a t=30


def test_missing_boundary_or_counter_reset_makes_integral_unknown(): ...
def test_observer_cost_excludes_registry_and_deployment():
    ...
    # memoria deployment=1000, observer=100, registry=500: observer=100,
    # deployment=1000; nessuna somma con la radice infra


def test_scheduler_scrape_preserves_raw_labels_and_histogram_buckets(): ...
def test_scheduler_http_error_keeps_cgroup_sampling_alive(): ...
def test_direct_reference_creates_no_scheduler_file_or_http_requests(): ...
def test_scheduler_options_must_be_supplied_together(): ...
def test_sigterm_flushes_a_final_sample_and_closes_open_files(): ...
def test_container_count_follows_the_scopes():
    samples = series_with_children(
        [["docker-a.scope"], ["docker-a.scope", "docker-b.scope"]]
    )
    assert [n for _, n in container_count(samples)] == [1, 2]
```

- [ ] **Step 2-4:** test che falliscono, implementazione, test che passano.
- [ ] **Step 5: Commit** — `feat: add the experiment-02 cgroup sampler`.

### Task 6: Generatore k6 condiviso e riuso del trasporto VM

**Files:**
- Modify: `packages/nanolab/src/nanolab/assets/k6/experiment-01.js` (unico script per entrambi gli esperimenti)
- Create: `packages/nanolab/src/nanolab/tasks/experiment_02/generator.py`
- Modify: `packages/nanolab/src/nanolab/comparison/prepare.py` (estrai il trasferimento verificato e riusalo in `stage_comparison`)
- Modify: `packages/nanolab/tests/comparison/test_experiment_01_generator.py`, `packages/nanolab/tests/comparison/experiment_01_k6_harness.mjs` (compatibilità dei default e stub degli import k6 necessari agli estrattori)
- Test: `packages/nanolab/tests/comparison/test_prepare.py` (trasferimento condiviso e checksum)
- Test: `packages/nanolab/tests/tasks/experiment_02/test_generator.py`, `packages/nanolab/tests/tasks/experiment_02/test_experiment_02_script.py` (salta se `k6` manca)

**Interfaces:**
- Consumes: `Experiment02Protocol` (Task 1), `comparison_remote_command`, `VmCommandProvider.exec_argv` e `VmFileFetcher.fetch_from` già esistenti.
- Produces:
  - `Vm(provider: VmCommandProvider, request: VmRequest)`, dataclass frozen in `generator.py`, soltanto i due campi, senza metodi di esecuzione o trasferimento;
  - `comparison_transfer_to(provider: VmCommandProvider, request: VmRequest, local: Path, remote: PurePosixPath) -> None` in `comparison/prepare.py`: estrazione del blocco già in `stage_comparison` che esegue `transfer_to`, verifica l'exit code e confronta lo sha256 remoto. Preparazione e generatore chiamano la stessa funzione, con gli stessi errori e controlli;
  - `K6Target(url: str, headers: Mapping[str, str], extractor: Literal["nanofaas-envelope", "json-base64", "raw-png"], field: str | None = None)`;
  - `generator_environment(protocol, body: bytes, target: K6Target) -> dict[str, str]`, con le chiavi esistenti `E01_PROTOCOL`, `E01_PAYLOAD` e la nuova `EXPERIMENT_TARGET`; quest'ultima porta target, `pngSha256` e `size`;
  - `run_generator(loadgen: Vm, *, script: Path, env: Mapping[str, str], remote_dir: PurePosixPath, local_dir: Path) -> int`, che restituisce il codice di uscita di k6 e recupera sempre gli eventi, anche se k6 fallisce.

Trasporto nei Task 6-8: `comparison_remote_command(vm.provider, vm.request, argv)` per i comandi che devono riuscire; `vm.provider.exec_argv(vm.request, argv, ...)` per esaminare l'esito senza eccezione, mantenendo il risultato Sonata originale; `comparison_transfer_to` per l'upload verificato e `VmFileFetcher(vm.provider, vm.request).fetch_from(...)` per il download. Nessuna seconda API `run/probe/put/get`, conversione in tuple o booleani che nascondano errori. I test iniettano un provider registrante tramite i campi di `Vm`.

Estendi soltanto `experiment-01.js`: calendario, eventi `send`/`done`, coorti e nomi degli export restano condivisi. `EXPERIMENT_TARGET` è opzionale; quando manca, rimangono URL da `NANOFAAS_URL`/`NANOFAAS_FUNCTION`, intestazione JSON, estrattore nanoFaaS e controllo leggero originali, con dimensione da `E01_PROTOCOL`. Quando presente cambia URL, intestazioni ed estrazione dell'output (`raw-png` usa `responseType: 'binary'`); con `pngSha256` impostato verifica anche l'hash del PNG. Il generatore experiment-02 passa il payload già adattato dalla piattaforma in `E01_PAYLOAD`.

La CLI experiment-02 passa e registra nell'identità lo stesso file `assets/k6/experiment-01.js`; non crea una seconda copia del calendario. Conserva la verifica dello sha256 dello script: un vecchio manifest con un hash diverso continua a richiedere una nuova directory di run, anche se i default funzionali sono compatibili.

- [ ] **Step 1: Scrivi i test**

```python
def test_generator_environment_serializes_protocol_target_and_body():
    ...
    # E01_PROTOCOL ed E01_PAYLOAD più EXPERIMENT_TARGET; nessun secondo formato


def test_shared_generator_preserves_experiment_01_defaults():
    ...
    # senza EXPERIMENT_TARGET: stessi scenari, URL/body/header, timeout,
    # gracefulStop, estrattore e ledger; conserva gli assert della suite esistente


def test_target_selection_does_not_change_calendar_or_send_cohorts(): ...
def test_verified_transfer_rejects_upload_failure_or_hash_mismatch():
    ...
    # stessa funzione usata da stage_comparison e dal generatore; errore prima di k6


def test_run_generator_fetches_events_even_when_k6_fails(tmp_path):
    provider = RecordingVmProvider(exit_codes={"k6": 99})
    loadgen = Vm(provider, LOADGEN_REQUEST)
    assert run_generator(loadgen, ...) == 99
    assert ("transfer_from", "/remote/k6-events.jsonl") in provider.calls


@pytest.mark.skipif(shutil.which("k6") is None, reason="k6 not installed")
def test_script_judges_each_extractor(fake_server):
    # server locale: busta nanoFaaS valida, JSON base64 valido, PNG grezzo valido,
    # PNG corrotto; calendario di 1 s per endpoint
    events = run_script(fake_server, ...)
    assert ok_by_endpoint(events) == {
        "envelope": True,
        "json": True,
        "raw": True,
        "corrupt": False,
    }
```

Questo test esegue solo lo script k6 contro un server finto: non è una prova della piattaforma.

- [ ] **Step 2-4:** test che falliscono, implementazione, test che passano; includi `tests/comparison/test_prepare.py`, `tests/comparison/test_experiment_01_generator.py` e `tests/plans/test_runtime_comparison.py` per verificare i chiamanti esistenti. Il test k6 del Task 6 esegue lo stesso script condiviso con tutti gli estrattori.
- [ ] **Step 5: Commit** — `feat: add the experiment-02 generator`.

### Task 7: Piattaforme — nanoFaaS P0/P1 e riferimento diretto

**Files:**
- Create: `packages/nanolab/src/nanolab/tasks/experiment_02/platforms.py`
- Test: `packages/nanolab/tests/tasks/experiment_02/test_platforms.py`

**Interfaces:**
- Consumes: `Vm`, `K6Target` (Task 6); `REGISTRY_CONTAINER` (Task 4).
- Produces:
  - `Platform` (Protocol) con:
    - `deploy(vm: Vm) -> None`;
    - `readiness(vm: Vm, *, timeout_seconds: int) -> dict[str, float] | None`, che restituisce `apiReadySeconds`, `registeredSeconds` e `firstCorrectSeconds` misurati sulla VM, oppure `None` allo scadere;
    - `target(host: str) -> K6Target`, `body(payload: bytes) -> bytes`;
    - `metrics_url: str | None`, URL locale sulla stack per il campionatore: `http://127.0.0.1:8081/actuator/prometheus` per nanoFaaS, `None` per il riferimento diretto;
    - `record(vm: Vm, evidence: Path) -> None`, `teardown(vm: Vm) -> None`;
  - `NanofaasPlatform(profile: Literal["P0", "P1"], control_plane_image: str, function_image: str, modules: tuple[str, ...], concurrency: int, queue_capacity: int, timeout_ms: int)`;
  - `DirectReference(function_image: str)`;
  - `leftover_containers(vm: Vm) -> list[str]`, cioè tutti i container tranne il registry.

Comandi:
- **P1:**
  - rete `nanolab-e02`;
  - `docker run -d --name e02-control-plane --user 0:0 --network nanolab-e02 -p 8080:8080 -p 8081:8081 -v /var/run/docker.sock:/var/run/docker.sock`, con l'ambiente del servizio `control-plane` di `deploy/compose/compose.yaml` adattato a questa rete (backend `container-local`, adapter `docker-java`, `CALLBACK_URL=http://e02-control-plane:8080/v1/internal/executions`, metriche `advanced`);
  - registrazione `{"name": "qr-code-rust", "image": <function_image>, "executionMode": "DEPLOYMENT", "concurrency": ..., "queueSize": ..., "maxRetries": 0, "timeoutMs": ...}`.
- **P0:**
  - container della funzione `e02-qr-code` sulla rete, con `PORT=8080`;
  - control plane senza socket Docker;
  - registrazione `EXTERNAL` con `endpointUrl: http://e02-qr-code:8080/invoke`, senza `queueSize`.
- **Diretto:** `docker run -d --name e02-qr-code -p 9000:8080 -e PORT=8080`; corpo `{"input": <payload>}`; estrattore `json-base64` sul campo `output`.
- **Readiness:** un ciclo `sh -c` sulla VM, ogni 0,5 s, che registra i tempi con `date +%s.%N`; sul Mac si verifica completamente l'ultima risposta.
- **`record`:** salva `docker inspect` di tutti i container e il `build-metadata` del control plane; se i moduli differiscono dalla configurazione solleva `ValueError`. Registra moduli e URL delle metriche per distinguere una metrica non prevista da una raccolta fallita; la serie temporale viene raccolta dal Task 5.
- **`teardown`:** cancella la funzione e rimuove tutti i container tranne il registry, compresi quelli creati dal provider; poi rimuove la rete.

- [ ] **Step 1: Scrivi i test** (con `Vm` e `RecordingVmProvider`)

```python
def test_p1_runs_the_docker_provider_and_registers_a_deployment_function():
    ...
    # argv con il socket Docker e container-local; JSON con DEPLOYMENT,
    # queueSize 200, maxRetries 0


def test_p0_registers_an_external_endpoint_without_the_docker_socket(): ...
def test_direct_reference_wraps_the_payload_as_runtime_input():
    body = DirectReference("img").body(b'{"text":"A","size":128}')
    assert json.loads(body) == {"input": {"text": "A", "size": 128}}


def test_teardown_removes_every_container_but_the_registry(): ...
def test_record_refuses_a_control_plane_with_unexpected_modules(tmp_path): ...
def test_metrics_url_is_present_only_for_nanofaas(): ...
```

- [ ] **Step 2-4:** test che falliscono, implementazione, test che passano.
- [ ] **Step 5: Commit** — `feat: add experiment-02 nanoFaaS and direct-reference platforms`.

### Task 8: Esecuzione di una cella e integrazione in `compare`

**Files:**
- Create: `packages/nanolab/src/nanolab/tasks/experiment_02/cell.py`
- Modify: `packages/nanolab/src/nanolab/comparison/target.py` (`read_comparison_target(..., *, kubernetes: bool = True)`: senza Kubernetes registra `engine: {"dockerId": ...}` da `docker info --format '{{.ID}}'`; `_validate_target` accetta `engine` al posto di `cluster`)
- Modify: `packages/nanolab/src/nanolab/cli/provisioning.py` (`_stack_operations`: per `loadProfile: experiment-02` esegue `plan_vm_provision_base`, `plan_registry_ensure_container`, `slice_setup_operation()` e la sincronizzazione del repository; niente k3s)
- Modify: `packages/nanolab/src/nanolab/cli/comparison.py` (ramo `experiment-02`)
- Test: `packages/nanolab/tests/tasks/experiment_02/test_cell.py`, `packages/nanolab/tests/cli/test_comparison.py`, `packages/nanolab/tests/cli/test_provisioning.py`

**Interfaces:**
- Consumes: tutto ciò che producono i Task 1-7; `attempt_dir`, `next_attempt`, `archive_cell_result`, `mark_load_started`, `write_cell_result(required=...)` con i tre artefatti comuni e `SCHEDULER` solo per nanoFaaS, come nel Task 2; trasporto riusato come nel Task 6.
- Produces: `run_cell(cell, *, stack: Vm, loadgen: Vm, platform: Platform, protocol, payload: bytes, script: Path, run_dir: Path, remote_root: PurePosixPath) -> Outcome`, dove `Outcome = Literal["measured", "startup_failed", "oom"]`; gli errori dopo `load-started.json` fermano la matrice, salvo un OOM documentato che diventa un risultato. Gli errori di cleanup vengono salvati in `diagnostics/cleanup-errors.json` e vietano di avviare un'altra cella.

Sequenza di `run_cell`:
1. rimuove i container avanzati e verifica che l'unità del campionatore precedente sia assente/inattiva; se resta una risorsa, solleva prima del nuovo deploy. Salva `diagnostics/cgroup-before.json` con identità del cgroup e `memory.events`, prima di applicare il budget o riavviare il motore;
2. avvia il campionatore con `systemd-run --collect --unit nanolab-e02-sampler --slice nanolab-infra-observers.slice`, passando `--interval 5` e i percorsi remoti specifici del tentativo; passa entrambe le opzioni scheduler solo quando `platform.metrics_url` è presente. Attende il primo campione completo e registra unità/PID/cgroup degli agenti;
3. applica il budget, riavvia il motore e rilegge il budget con `parse_budget`; se differisce, conserva l'errore e passa al cleanup senza avviare il deployment. Il campionatore fuori budget osserva anche eventuali OOM di dockerd/containerd in questo passaggio;
4. `deploy`, poi `readiness`; se scade, registra il timeout senza restituire ancora un esito e senza lanciare il carico;
5. solo con readiness riuscita: controlla `processes_outside`, esegue `record`, poi `mark_load_started` e `run_generator`. Un codice k6 diverso da zero diventa un errore di esecuzione da classificare dopo la raccolta delle evidenze;
6. in un `finally` che copre l'intero tentativo, prima di rimuovere i container: legge `memory.events` via SSH, senza dipendere da Docker, in `diagnostics/cgroup-after.json`; salva inspect, `docker ps -a`, log limitati e gli errori delle sonde. Conserva `State.OOMKilled` dei soli container di questo tentativo. Le sonde hanno timeout e il fallimento di una non impedisce le altre;
7. nello stesso cleanup, anche su timeout, eccezione o interruzione: ferma il campionatore, attende la chiusura e verifica l'unità inattiva/rimossa; recupera sempre `SAMPLES` e, per nanoFaaS, `SCHEDULER`, inclusi i file parziali. Non cerca file scheduler per il riferimento diretto. Tenta anche il recupero degli eventi/summary k6 già prodotti. Se la partenza del servizio ha avuto esito incerto, ne tenta comunque arresto e verifica;
8. esegue sempre `teardown`, anche se stop o recupero sono falliti. Ogni operazione di cleanup viene tentata separatamente; gli errori secondari vanno nelle diagnostiche e non sostituiscono l'eccezione originale;
9. dopo la raccolta, un incremento OOM rispetto all'inizio del tentativo, o un container del tentativo con `OOMKilled`, determina `oom` anche durante avvio, riavvio del motore o errore del generatore. Confronta gli estremi prima del teardown, senza attribuire alla nuova cella i contatori delle precedenti. Evidenza mancante/reset non documentato significa OOM ignoto, non zero: in assenza di altra prova sufficiente, ferma la matrice con evidenze parziali;
10. senza OOM documentato: un timeout di readiness verificato produce `startup_failed`; gli altri errori vengono rilanciati; solo un carico terminato normalmente con gli artefatti richiesti produce `measured`. Nessun ritorno anticipato salta il cleanup. Un'interruzione dell'operatore resta un'interruzione, senza avviare altre celle.

In `compare`: accetta `experiment-02` con backend `container` su Azure con i ruoli `stack` e `loadgen`; `--variants` elenca le chiavi di configurazione; le immagini arrivano dalle distribuzioni preparate. Il ciclo dei tentativi e delle ricevute segue `_run_experiment_cell`, ma l'esito arriva da `run_cell` invece che dall'elenco dei pod. Un errore di trasporto prima del carico, con la piattaforma verificata pronta prima del teardown, si ritenta una volta soltanto dopo cleanup riuscito. OOM e startup falliti non si ritentano. Se il cleanup fallisce, conserva comunque la ricevuta di un `oom`/`startup_failed` documentato e poi interrompe la matrice; non emette `measured` né ritenta finché lo stato remoto non è pulito. La CLI controlla `diagnostics/cleanup-errors.json` prima di passare alla prossima cella.

- [ ] **Step 1: Scrivi i test** (`Vm`, `RecordingVmProvider` e piattaforma finta)

```python
def test_cell_measures_in_order(tmp_path): ...  # sequenza dei comandi 1-10
def test_readiness_timeout_is_startup_failed_with_diagnostics(tmp_path): ...
def test_oom_kill_during_load_is_oom(tmp_path): ...
def test_oom_before_readiness_is_oom(tmp_path):
    ...
    # readiness scaduta + incremento oom_kill: oom, con diagnostiche e ricevuta


def test_engine_oom_during_restart_is_oom(tmp_path):
    ...
    # Docker non risponde, ma memory.events via SSH prova l'OOM: oom


def test_oom_with_generator_error_is_oom(tmp_path): ...
def test_unknown_oom_evidence_stops_without_a_measured_receipt(tmp_path): ...
def test_budget_mismatch_stops_before_deploy(tmp_path): ...
def test_process_outside_the_slice_stops_before_load(tmp_path): ...
def test_leftover_containers_are_removed_before_deploy(tmp_path): ...
def test_teardown_runs_even_when_k6_fails(tmp_path): ...
def test_next_cell_can_start_after_readiness_timeout(tmp_path):
    ...
    # due tentativi consecutivi: sampler fermato, file recuperati e unità
    # liberata prima del secondo start; nessuna scrittura nel vecchio tentativo


def test_sampler_stop_and_fetch_run_after_each_failure(tmp_path):
    ...
    # parametrizzare timeout, errore deploy, processo esterno, k6 e interruzione


def test_cleanup_errors_preserve_the_original_exception(tmp_path):
    ...
    # fetch_from fallisce: stop e teardown avvengono comunque; errore iniziale preservato


def test_cleanup_failure_records_oom_but_stops_the_matrix(tmp_path): ...
def test_nonzero_k6_without_oom_never_produces_measured(tmp_path): ...
def test_experiment_02_stack_has_docker_and_registry_but_no_k3s(): ...
def test_target_without_kubernetes_records_the_docker_engine(): ...
def test_compare_rejects_experiment_02_on_kubernetes_or_without_loadgen(): ...
def test_compare_direct_and_p1_with_two_budgets_can_resume(tmp_path):
    ...
    # CLI con VM finte: capture/new/write/read manifest, prepare condiviso,
    # quattro celle, ricevute e resume; nessun accesso a direct-reference.recipe
```

- [ ] **Step 2-4:** test che falliscono, implementazione, test che passano, più tutta `tests/comparison` e `tests/cli`.
- [ ] **Step 5: Commit** — `feat: run experiment-02 cells through compare`.

### Task 9: Riepilogo per cella e fase, con efficienza

**Files:**
- Modify: `packages/nanolab/src/nanolab/comparison/experiment_02.py`
- Test: `packages/nanolab/tests/comparison/test_experiment_02_summary.py`

**Interfaces:**
- Consumes: `summarize_phases` esistente con `snapshot=None`; `working_set`, `cpu_cores`, `container_count`, `window_covered`, `cpu_seconds`, `memory_byte_seconds`, `oom_kills` (Task 5), `SCHEDULER` quando applicabile (Task 2).
- Produces:
  - `summarize_experiment_02_cell(cell_dir: Path, protocol) -> dict[str, object]`: le righe di ledger e verdetti da `summarize_phases(snapshot=None)`, completate con le statistiche cgroup del deployment, `cpuSecondsPerUseful`, `memoryByteSecondsPerUseful`, `efficiencyStartSeconds`, `efficiencyEndSeconds`, medie/massimi del motore e degli agenti, `containersMean`, `containersMax`, stato di copertura per risorsa e tempi di readiness. Per lo scheduler riporta `schedulerCoverage` (`complete`, `partial`, `not_applicable`) e il percorso delle serie grezze, `None` per il riferimento diretto;
  - `write_experiment_02_report(root: Path, cells, protocol) -> Path`: `experiment-02-summary.json` e `.csv`, con colonne `configuration` e `budget` e gli stessi campi di misura/copertura; le celle senza ricevuta sono `incomplete`, quelle `oom`/`startup_failed` conservano causa ed evidenze senza valori di prestazione.

Mantieni invariata la firma di `summarize_phases`: senza snapshot riusa già calendario, ledger, latenze e verdetti e lascia ignote le risorse. Calcola una sola volta media, massimo e delta delle risorse direttamente dai campioni validati del Task 5, assegnandoli ai campi delle righe. Il motore somma i due servizi disgiunti soltanto sui campioni coperti da entrambi; gli observer usano `@observers`. Nessuna serializzazione dei punti in snapshot e successiva rilettura o correzione di medie parziali.

Finestre, in secondi dall'origine del ledger k6:

- per una fase con inizio `s` e durata `T`, medie/massimi e conteggi usano `[s, s + T]`; le medie CPU conservano la regola esistente che esclude valori la cui finestra retrospettiva di 30 s attraversa l'inizio della fase;
- per ogni fase di carico, inclusa la warmup, l'efficienza usa `[s, s + T + protocol.drain_seconds]`. Il vincolo ereditato `drainSeconds >= gracefulStopSeconds` copre tutte le risposte ancora osservabili e l'ultima finestra termina alla fine del drain del calendario. Non si accorcia la finestra all'ultima risposta effettiva;
- `cpuSecondsPerUseful = cpu_seconds(..., start, end) / withinThreshold`, e analogamente per l'integrale della memoria. Il denominatore resta quello della coorte di invio, anche per risposte completate nella fase successiva. Con denominatore zero o integrale ignoto il valore è `None`;
- queste sono misure del deployment su finestre dichiarate: possono includere lavoro di coorti vicine, sovrapporsi anche a più fasi e non sono additive né un'attribuzione esclusiva del costo alla coorte. Non cambiare calendario o ledger per nascondere questa limitazione.

Prima di calcolare ciascuna media, massimo, conteggio o integrale, applica `window_covered` ai campi sorgente della relativa risorsa. Un buco oltre 10 s, un estremo mancante o un reset rende ignoti i valori interessati e i delta che li usano; conserva numero di campioni e motivo. Il motore richiede copertura di entrambi i servizi; gli observer hanno una copertura propria. Per i conteggi è necessaria una scansione completa dei figli, mentre l'assenza verificata di un container è un conteggio zero valido. Ledger e verdetti di servizio rimangono utilizzabili quando sono integri. Le API e i default dell'esperimento 1 mantengono il comportamento precedente.

Per nanoFaaS valuta `schedulerCoverage` sulla finestra idle-carico-drain, usando timestamp, esito degli scrape e intervallo atteso di 5 s: un errore o una lacuna rende la raccolta parziale. Conserva l'esposizione completa, comprese le serie del catalogo relative ai moduli effettivamente presenti, senza convertire assenze in zero. Per il riferimento diretto ricava `not_applicable` dalla configurazione identificata dalla ricevuta, senza leggere o richiedere un file scheduler. Le righe JSON/CSV espongono separatamente CPU/memoria degli agenti; i loro costi non si sottraggono né si aggiungono al deployment.

- [ ] **Step 1: Scrivi i test**

```python
def test_service_verdicts_are_reused_without_a_resource_snapshot(tmp_path):
    ...
    # ledger identico: stessi valori di servizio di summarize_phases(snapshot=None);
    # workingSetBytes/cpuCores e delta provengono soltanto dai campioni cgroup


def test_efficiency_divides_by_responses_within_threshold(tmp_path): ...
def test_efficiency_includes_drain_and_cross_boundary_responses(tmp_path):
    ...
    # fase [0,10], drain=5, CPU=2 core e memoria=100 byte su [0,15];
    # due risposte utili della coorte, una a t=11: CPU/useful=15 s,
    # memoria/useful=750 byte*s; estremi salvati 0 e 15


def test_last_efficiency_window_ends_at_calendar_drain_end(tmp_path): ...
def test_efficiency_is_unknown_when_only_the_drain_has_a_gap(tmp_path): ...
def test_efficiency_is_undefined_without_useful_responses(tmp_path):
    assert phase(summary, "step-01")["cpuSecondsPerUseful"] is None


def test_engine_share_is_reported_separately(tmp_path): ...
def test_observer_cost_is_reported_separately_in_json_and_csv(tmp_path): ...
def test_gap_invalidates_resource_means_and_maxima(tmp_path):
    ...
    # campioni t=0,5,30: mean/max/counts ignoti, motivo di copertura presente;
    # il ledger integro mantiene withinThreshold e i verdetti di servizio


def test_invalid_idle_coverage_makes_resource_deltas_unknown(tmp_path): ...
def test_scheduler_gap_is_partial_and_direct_reference_is_not_applicable(tmp_path):
    ...
    # diretto senza file SCHEDULER: not_applicable e percorso None;
    # nanoFaaS con errore/lacuna: partial, mai not_applicable


def test_failed_cells_have_diagnostics_but_no_performance_values(tmp_path): ...
def test_report_lists_incomplete_cells_without_values(tmp_path): ...
```

- [ ] **Step 2-4:** test che falliscono, implementazione, test che passano, più i test dell'esperimento 1.
- [ ] **Step 5: Commit** — `feat: summarize experiment-02 cells with per-response efficiency`.

### Task 10: Documentazione, qualità e prova su Azure con sole celle nanoFaaS

**Files:**
- Create: `docs/experiment-02.md` (in inglese: stato, protocollo, budget e slice, campionatore, esiti, comandi, limiti)
- Modify: `packages/nanolab/README.md` (collegamento)

Limiti da scrivere nella documentazione:
- l'efficienza usa la finestra degli arrivi più `drainSeconds`, con gli estremi del Task 9; può sovrapporsi a più fasi, include il lavoro concorrente delle altre coorti e non si somma fra fasi;
- il motore Docker è dentro il budget ed è riportato a parte;
- gli agenti sono fuori budget, misurati separatamente senza registry; le serie dello scheduler sono conservate grezze, con copertura esplicita e assenze distinte da zero;
- buchi di campionamento invalidano anche medie/massimi e delta; un errore di cleanup blocca le celle successive preservando le evidenze e gli OOM documentati;
- la readiness è campionata ogni 0,5 s.

- [ ] **Step 1: Controlli di qualità**

```bash
uv run --frozen ruff check packages
uv run --frozen --all-packages --all-groups basedpyright --project packages/nanolab
uv run --frozen --all-packages --all-groups lint-imports --config packages/nanolab/.importlinter --no-cache
uv run --frozen --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/comparison packages/nanolab/tests/config packages/nanolab/tests/cli packages/nanolab/tests/tasks/experiment_02 -q
```

Expected: tutto pulito; i 436 fallimenti noti del Mac restano fuori da queste aree. La suite completa va eseguita su Linux, in CI o sulla VM di sviluppo `nanolab`.

- [ ] **Step 2: Prova su Azure** (autorizzata dalla spec)

```bash
export NANOFAAS_ROOT=$PWD/.nanofaas-source
./nanolab.sh compare experiment-02-pilot.yaml \
  --environment /Users/micheleciavotta/Downloads/nanolab/packages/nanolab/environments/azure-comparison.yaml \
  --variants nanofaas-p1-jvm,direct-reference --repetitions 1 \
  --run-dir runs/experiment-02-foundation-smoke
```

Expected: due ricevute `measured`; JSON e CSV riportano deployment, motore e agenti con copertura completa, costi separati ed efficienza sulle finestre comprensive del drain per le fasi con risposte utili. La ricevuta P1 include le serie grezze dello scheduler raccolte per tutta la misura; il report diretto dichiara `not_applicable` e la relativa ricevuta non richiede un file scheduler. Entrambe le celle usano lo script k6 condiviso, con hash registrato. Dopo ogni cella il campionatore è fermo e non restano file scritti dal tentativo precedente. Verificare inoltre il resume della stessa run: nessuna nuova cella né nuova build. Se l'MFA blocca il provisioning, fermati e chiedi all'autore di autenticarsi.

- [ ] **Step 3: Riporta le VM create** — elenca nome, taglia e gruppo di risorse, con il comando di cleanup di `docs/experiment-01.md`. Non eliminarle senza conferma.
- [ ] **Step 4: Commit** — `docs: describe experiment 02 and its first Azure run`.
