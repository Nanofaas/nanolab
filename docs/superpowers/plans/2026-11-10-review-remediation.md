# Sonata, Nanolab e Workbench — piano di correzione e verifica

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans per eseguire questo piano per task. Usare superpowers:subagent-driven-development soltanto se viene scelta l'esecuzione delegata. Le checkbox indicano attività da completare, non verifiche già eseguite.

**Goal:** correggere i dieci rilievi della revisione, preservando i dati in caso di errore e verificando il comportamento su Linux, Docker e Kubernetes.

**Architecture:** mantenere la separazione attuale: Sonata possiede il ciclo di vita; workbench definisce i componenti; Nanolab decide collocazione, supervisione e validità delle misure. Correggere i contratti condivisi e i relativi chiamanti; unificare soltanto la politica di conservazione dei collector duplicata. Nessun nuovo framework o dipendenza applicativa.

**Tech Stack:** Python/pytest/uv, Sonata, Ansible, Docker, Helm/kind, Multipass; Azure per prove che superano le risorse locali o richiedono il provider cloud.

**Spec:** revisione del 10 novembre 2026, riportata nella matrice seguente; vincoli di prodotto in `../specs/2026-11-08-nanofaas-workbench-design.md` e disciplina di rilascio in `CLAUDE.md`. Le scelte correttive sotto sono proposte tecniche del piano; nessuna implementazione o pubblicazione è già stata effettuata.

## Perimetro e baseline

Repository fratelli sotto `/Users/micheleciavotta/Downloads/`:

| Repository | Commit esaminato | Area |
| --- | --- | --- |
| `sonata` | `3a08cc6` | risorse task, composite, compensazione, resume |
| `nanofaas-workbench` | `150037d` | collection 0.1.1, supervisor, container e pod |
| `nanolab` | `742c0d39` | migrazione Sonata 0.7.0 e piattaforma one-shot |

| Rilievo | Comportamento da correggere | Task |
| --- | --- | --- |
| R1 | errore archivio collector non protegge la VM dalla cancellazione | N1 |
| R2 | errore del journal dopo acquire lascia un membro vivo | S1 |
| R3 | container del generatore e osservatori operatore senza CPU placement | N2 |
| R4 | assenza totale di metriche container accettata come copertura completa | N3 |
| R5 | wrapper nascondono fingerprint e cambiamenti al resume | S2 |
| R6 | Service Kubernetes non raggiunge il bind loopback | W2 |
| R7 | k6 con hostPath può essere schedulato sul nodo sbagliato | W3 |
| R8 | raccolta di istanze omonime su host diversi sovrascrive gli artefatti | W1, N4 |
| R9 | confronto aggregato nasconde un trial mancante | N5 |
| R10 | kind JSON non stringa produce eccezione invece di HTTP 400 | W4 |

## Vincoli globali

- `NANOFAAS_ROOT` resta in sola lettura. Usare copie/archivi isolati anche per build e contratti.
- Nessuna cancellazione di VM o dati prima dell'esportazione verificata delle evidenze; in caso di archivio fallito conservarli e registrare il recupero necessario.
- `sonata-engine` e `sonata-tasks` mantengono la stessa versione pubblicata; nessun `git+` in `nanolab/uv.lock`. Prima della pubblicazione, provare le wheel candidate in un ambiente temporaneo senza alterare i pin distribuiti.
- Conservare i default della forma container. Le modifiche incompatibili della forma pod e dei percorsi di raccolta vanno documentate e rilasciate insieme ai loro chiamanti.
- Proposta di versione: Sonata `0.7.1`, workbench `0.2.0`, previa verifica che non siano già occupate al momento del rilascio. Non pubblicare come parte della sola pianificazione.
- Un test rosso deve fallire per il comportamento identificato, non per Docker spento, pacchetti mancanti o permessi della sandbox.
- Le prove Linux obbligatorie non possono risultare verdi tramite skip; conservare riepilogo pytest, skip e versioni effettive.
- Test di errori di disco/journal: iniezioni mirate o filesystem temporanei limitati; mai riempire il disco dell'operatore.
- Nessun refactoring generale di `platform.py`, nuova API di placement o nuova piattaforma di test. Commit separati per correzioni verificabili.

## Review focus

Cinque condizioni trasversali che i nuovi test devono coprire:

1. Errore di registrazione dopo un effetto esterno riuscito: cleanup una sola volta, errore originale conservato (S1).
2. Fallimento dell'archivio seguito da un secondo errore di export: la protezione della VM deve essere già attiva (N1).
3. Stesso nome di istanza su host diversi, anche in raccolte sequenziali: nessuna sovrascrittura (W1/N4).
4. Endpoint raggiungibile e scrape `up=1`, ma dati container assenti: copertura incompleta (N3).
5. Pod Pending o schedulato lontano dai file: deploy non deve dichiarare il generatore avviato (W3).

## Ordine e dipendenze

Eseguire prima **S1 e N1** per la sicurezza del cleanup, poi **N2** per la validità delle misure. Completare **S2, W1–W4, N3–N5**, quindi il collaudo **V1**. W1 definisce il formato consumato da N4; le altre correzioni possono essere sviluppate separatamente, senza condividere ambienti di test attivi. Integrare N4 con il candidato workbench, mai con il vecchio pin spacciato per quello nuovo.

### S1 — Registrare la proprietà prima del journal terminale

**Repository/files:** `sonata/src/sonata_engine/core/{compose,workflow,step_scope}.py`; test `sonata/tests/core/test_compose.py`, `test_resource_cleanup.py`, `test_steps_resume.py`.

**Interfaccia:** estendere il metodo privato `run_step(step, slug, upstream, *, on_outcome: Callable[[TaskOutcome[Any]], None] | None = None) -> TaskExecution` nello scope e nel suo Protocol. Inoltrare il callback a `_execute_recorded`; i chiamanti esistenti restano validi. `_AcquireMembers` registra valore e proprietà nel callback.

- [ ] Aggiungere `test_member_is_released_when_record_passed_fails`: acquire A/B riusciti, scrittura terminale di B fallita; `assert releases == ['B', 'A']` e errore originale presente. Includere membro con valore `None`, composite annidato e un release che fallisce senza impedire gli altri.
- [ ] Eseguire il test sul codice attuale: deve mancare il rilascio del membro appena acquisito.
- [ ] Implementare il callback evitando doppia registrazione dopo il ritorno da `run_step`. Aggiornare tutti gli scope/doppi di test che implementano il Protocol, trovati con `rg`.
- [ ] Eseguire `uv run --locked --all-packages --all-groups --all-extras pytest -c pyproject.toml tests/core/test_compose.py tests/core/test_resource_cleanup.py tests/core/test_steps_resume.py`; tutti verdi, comprese le regressioni sul journal durante release.
- [ ] Commit isolato `fix: release composite members after terminal journal failure`.

### N1 — Conservare la VM quando il collector non è archiviabile

**Repository/files:** `nanolab/packages/nanolab/src/nanolab/workbench/platform.py`, `tasks/one_shot/logging.py`, `one_shot/infrastructure.py` solo se necessario; test `tests/workbench/test_platform.py`, `tests/one_shot/test_logging.py`, `tests/one_shot/test_resources.py`.

**Interfaccia:** usare `NodeResource.recovery` esistente. Estrarre in `tasks/one_shot/logging.py` una funzione condivisa `retain_collector_for_recovery(node, *, run_dir: Path, storage: str, error: Exception) -> None`; prima aggiunge il motivo alla guardia, poi tenta la ricevuta. Non spostare l'intero teardown.

- [ ] Aggiungere `test_collector_archive_failure_preserves_owned_vm`: fault in copia/checksum, poi teardown completo; `assert node.recovery`, `assert deleted_vms == []`, gli altri membri sono rilasciati. Il test deve attraversare la guardia reale di `_owned_node`, non soltanto un VM stub privo di cleanup.
- [ ] Aggiungere varianti: stop collector fallito; export successivo fallito; ricevuta non scrivibile; percorso riuscito che invece rilascia la VM. La causa iniziale deve restare leggibile.
- [ ] Eseguire i test e osservare il rosso sul codice precedente; chiamare la funzione condivisa sia dal vecchio collector sia dal workbench prima di qualunque export successivo.
- [ ] Eseguire `uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/workbench/test_platform.py packages/nanolab/tests/one_shot/test_logging.py packages/nanolab/tests/one_shot/test_resources.py --no-cov`.
- [ ] Commit `fix: retain collector VM when raw archive fails`.

### N2 — Applicare e verificare il CPU placement ai container dell'operatore

**Files:** `nanolab/.../workbench/{loadgen,platform}.py`, eventuali verifiche in `one_shot/host_cpu.py`; test `tests/workbench/{test_loadgen,test_platform}.py`, `tests/one_shot/test_host_cpu_placement.py`. `...` nei task Nanolab indica `packages/nanolab/src/nanolab`.

**Contratto:** quando `settings.host_cpu_placement` esiste, k6 e gli osservatori dell'operatore usano `operator_cpus`; Loki mantiene `logging_cpus` o `OperatorLogging.cpus` nei multisite. Non applicare gli ID CPU dell'host ai container dentro le VM. Senza placement esplicito conservare il comportamento non vincolato.

- [ ] Test `test_loadgen_uses_operator_cpuset` e `test_operator_observers_respect_reserved_cpus`: input CPU operatore `[0, 1]`, Loki `[2, 3]`; `assert k6.cpuset == '0,1'`, stesso per Prometheus/supervisor/exporter operatore, Loki `'2,3'`; nessun overlap con le CPU riservate alle VM.
- [ ] Farli fallire; propagare il campo `Instance.cpuset` già supportato. Usare `_inspect` e le verifiche già presenti per confrontare placement atteso ed effettivo prima delle misure; un mismatch deve interrompere l'avvio.
- [ ] Verificare anche i chiamanti capacity/qualification/experiment: devono passare `settings` e non perdere il vincolo nel binding `partial`.
- [ ] Eseguire i tre file pytest sopra con il comando Nanolab standard `--no-cov`; in V1 verificare anche `docker inspect` e `Cpus_allowed_list` dei processi reali.
- [ ] Commit `fix: preserve operator CPU placement for workbench containers`.

### S2 — Propagare i fingerprint attraverso i wrapper

**Files:** `sonata/packages/sonata-tasks/src/sonata_tasks/{compensation,compose,helm}.py`; test `tests/test_compensation.py`, `test_compose.py`, `test_helm.py`, `test_fingerprint.py` sotto lo stesso package; `sonata/tests/test_resume.py` se serve il fixture del journal.

**Interfaccia:** override `_fingerprint_payload(self) -> object`, con tipo e payload dei task contenuti secondo la forma già usata da `Steps`; nessun hash di `repr`, indirizzi di memoria o callable.

- [ ] Test: cambiando `reuse_key` dell'acquire, `pre_clean`, `build` o `ready_url`, `assert old_fingerprint != new_fingerprint`; ricostruendo configurazione identica, uguaglianza. Verificare analogamente il wrapper Helm.
- [ ] Test resume: journal precedente e wrapper modificato devono essere rifiutati dalla validazione topologica; il consumer riusabile non deve essere silenziosamente saltato.
- [ ] Eseguire rosso, poi implementare propagazione in tutti i livelli che oggi la interrompono. Documentare che i journal interessati richiedono un nuovo run o completamento con la versione precedente.
- [ ] Eseguire `uv run --locked --all-packages --all-groups --all-extras pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests/test_compensation.py packages/sonata-tasks/tests/test_compose.py packages/sonata-tasks/tests/test_helm.py packages/sonata-tasks/tests/test_fingerprint.py`.
- [ ] Commit `fix: preserve wrapped task fingerprints during resume`.

### W1 — Rendere gli archivi univoci per host

**Files:** `nanofaas-workbench/roles/common/tasks/fetch_dir_tarball.yml`, `roles/log_collector/tasks/collect.yml`, eventuale inizializzazione condivisa in `playbooks/tasks/run_instances.yml`; documentazione `docs/vars.md`, README dei ruoli; `tests/integration/test_collect_stub.py` e nuovo `tests/integration/test_collect_multihost.py`.

**Contratto prodotto:** tutti gli artefatti collect vanno in `<collect_dir>/<inventory_hostname>/<instance.name>/...`, anche per un solo host. Non usare un formato che cambia con il numero degli host. I nomi dei file restano invariati. Rifiutare nomi host non utilizzabili come singolo segmento di percorso, invece di consentire traversal o normalizzazioni che causano collisioni.

- [ ] Test multihost con stessa istanza `lk`, contenuti distinti `host-a`/`host-b`: due tar validi, entrambi presenti, contenuto e checksum corretti. Ripetere la raccolta con invocazioni separate e verificare che il primo rimanga intatto.
- [ ] Test analogo per `metrics.txt` e test negativo con alias contenente separatori di percorso.
- [ ] Eseguire rosso; centralizzare il calcolo della directory nel percorso comune ai ruoli, applicarlo a tarball e metriche; aggiornare tutti gli assert dei percorsi cercando `collect_dir`, `snapshot.tar.gz`, `storage.tar.gz`, `metrics.txt`.
- [ ] Eseguire `uv run --locked pytest -q tests/integration/test_collect_stub.py tests/integration/test_collect_multihost.py` su Linux con Docker. Nessuno skip accettato per questi test.
- [ ] Commit `fix: namespace collected evidence by inventory host` e nota di migrazione del percorso.

### W2 — Rendere raggiungibili i Service senza esporre l'admin API sull'host

**Files:** `nanofaas-workbench/roles/{prometheus,supervisor}/tasks/deploy.yml`, `chart/templates/{prometheus,supervisor}.yaml`, `chart/values.yaml`, README dei due ruoli; `tests/test_chart.py`, `tests/integration/test_pod_form.py`.

**Scelta proposta:** Prometheus e supervisor in forma pod usano la rete del pod, bind predefinito `0.0.0.0` e Service ClusterIP; eliminare `hostNetwork` per questi due componenti. La forma container conserva loopback e host network. In forma pod disabilitare l'admin API di Prometheus: collect pod è già esplicitamente non supportato. Gli URL loopback verso servizi del nodo devono diventare indirizzi espliciti/Service nella configurazione pod. Un bind loopback esplicito incompatibile col Service deve essere rifiutato chiaramente.

- [ ] Test chart: rete pod, Service corretto, readiness sulla porta del pod, admin API assente nella forma pod; container mantiene argomenti precedenti.
- [ ] Test reale da un pod client separato: `http://wb-pr:9090/-/ready` e `http://wb-supervisor:8099/status` rispondono; il supervisor interroga Prometheus via DNS del Service. Non usare `kubectl exec` nel server su loopback come prova di raggiungibilità.
- [ ] Test che `POST /api/v1/admin/tsdb/snapshot` non riesca in forma pod e che non siano creati hostPort/NodePort/LoadBalancer. Verificare separatamente che collect container continui a funzionare.
- [ ] Far fallire, correggere, eseguire `uv run --locked pytest -q tests/test_chart.py` e in V1 `uv run --locked pytest -q -m kind tests/integration/test_pod_form.py`.
- [ ] Commit `fix: align pod services with listener and network configuration`.

### W3 — Vincolare il loadgen al nodo che possiede dati e endpoint

**Files:** `nanofaas-workbench/roles/loadgen/tasks/deploy.yml`, `chart/templates/loadgen.yaml`, `chart/values.yaml`, `roles/loadgen/README.md`; `tests/integration/test_pod_roles.py`, `test_pod_form.py`, `tests/test_chart.py`.

**Contratto:** `config.node_name` obbligatorio per loadgen pod e propagato a `spec.template.spec.nodeName`. Mantiene host network perché gli schedule possono usare il loopback del nodo. `output_dir` e input hostPath devono esistere sul nodo selezionato: nella forma pod non crearli sul semplice host Ansible, che potrebbe essere solo un controller. Usare il mount `Directory` e verificare l'avvio effettivo; provisioning dei dati a carico del chiamante, documentato.

- [ ] Test ruolo/chart: `node_name` mancante rifiutato, valore corretto propagato; aggiornare i fixture della forma pod.
- [ ] Test kind con almeno due nodi: dati disponibili soltanto sul worker prescelto, Job effettivamente su quel worker, output e richieste corretti. Preparare il percorso con `extraMounts` nel config kind, senza presupporre che il filesystem della VM coincida con quello del nodo-container kind.
- [ ] Test negativo: directory assente o nodo non esistente; deploy fallisce entro il timeout senza accettare il solo contatore `.status.active`. Attendere Pod realmente Running, oppure Job già Completed con successo; fallire su Failed e su timeout Pending.
- [ ] Test retry: Job già concluso non riparte; uno in corso non duplica il carico.
- [ ] Eseguire rosso/verde con `uv run --locked pytest -q tests/test_chart.py tests/integration/test_pod_roles.py`; test reali nel comando kind di W2. Commit `fix: schedule loadgen on its data-owning node`.

### W4 — Validare il tipo del target prima della ricerca

**Files:** `nanofaas-workbench/supervisor/src/workbench_supervisor/checks.py`; `supervisor/tests/{test_checks,test_server}.py`.

- [ ] Parametrizzare `kind` con `[]`, `{}`, `None`, `False`, `1`: `validate` solleva `ValueError`, HTTP PUT restituisce 400; una successiva GET `/status` funziona e nessun target invalido è stato aggiunto.
- [ ] Eseguire rosso; aggiungere il controllo di tipo prima del lookup. Non catturare genericamente `TypeError` nell'intero server nascondendo errori interni.
- [ ] Eseguire `uv run --locked pytest -q supervisor/tests/test_checks.py supervisor/tests/test_server.py`; commit `fix: reject non-string target kinds with HTTP 400`.

### N3 — Non confondere assenza dei container con copertura completa

**Files:** `nanolab/.../workbench/vm_export.py`, `one_shot/vm_resources.py` soltanto se necessario; test `tests/workbench/test_vm_export.py`, `tests/one_shot/test_vm_resources.py`, `tests/one_shot/test_experiment_task.py`.

**Contratto:** i nomi obbligatori sono quelli di `NodeRoles.roles`; se non compaiono mai, produrre un errore nello schema `errors` già letto da `summarize_resources`. Non inventare container ID e non trasformare ogni funzione dinamica in un container atteso per tutta la finestra. Mantenere il controllo dei gap per i container comparsi.

- [ ] Fixture: metriche VM presenti e `up=1`, tutte le serie container assenti; `assert coverage['complete'] is False`. Ripetere con solo control plane assente, collector obbligatorio assente e finestra con dati normali.
- [ ] Test cambiamento di incarnation/container ID e funzione che nasce dopo l'inizio: non introdurre falsi errori per lifecycle legittimi; un ruolo obbligatorio assente deve restare riconoscibile nel report.
- [ ] Test scientific gate: copertura incompleta rende invalide le evidenze, anche senza un fault del supervisor.
- [ ] Eseguire rosso/verde con pytest sui tre file sopra, comando Nanolab standard `--no-cov`; commit `fix: reject resource coverage missing mandatory containers`.

### N4 — Adeguare Nanolab al formato host/istanza

**Dipendenza:** W1. **Files:** `nanolab/.../workbench/{playbook,platform}.py`, `components.py` se necessario; test `tests/workbench/{test_playbook,test_components,test_platform}.py`; documentazione dei percorsi in `packages/nanolab/README.md`.

**Contratto:** usare alias Ansible stabili: `localhost` per l'operatore e `WorkbenchHost.key` per i nodi. Per i nodi, costruire un inventario JSON con alias e `ansible_host` reale dopo la risoluzione/retarget del provider, preservando utente, porta, chiave e ProxyJump. Così i lettori usano `<workbench>/<alias>/<instance>/...` senza dedurre l'identità da un IP o cercare casualmente file vecchi.

- [ ] Test inventario per operatore, Multipass diretto, Azure e sito con ProxyJump: alias stabile, endpoint e opzioni SSH invariati. Due siti con lo stesso IP privato devono produrre directory distinte.
- [ ] Test piattaforma: metriche collector e snapshot letti dal nuovo percorso; uno snapshot mancante non autorizza la cancellazione della TSDB; errore di undeploy continua a conservarla.
- [ ] Far fallire; aggiornare WorkbenchPlaybook e i due lettori in `platform.py` (metriche collector e `drop_tsdb`) insieme ai fixture. Niente fallback silenzioso al vecchio percorso per nuovi run; gli archivi storici restano leggibili dai report esistenti.
- [ ] Eseguire `uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/workbench --no-cov` con collection candidata isolata; commit `fix: consume workbench evidence through stable host aliases`.

### N5 — Confrontare le evidenze per trial e percorso

**Files:** `nanolab/scripts/compare-run-shape.py`, `packages/nanolab/tests/scripts/test_compare_run_shape.py`.

**Contratto:** identità dei file = percorso relativo alla radice del run; identità di coverage = percorso relativo del file più nodo. Le mediane restano per file, non aggregate fra trial omonimi. Usare confronti di schema per lo stesso scope/trial; una union globale non può compensare dati mancanti altrove.

- [ ] Test run con radice e `capacity/trial-0`: eliminare il trial solo da NEW; `assert differences` e messaggio che nomina `capacity/trial-0`.
- [ ] Test due trial con gli stessi nodi ma serie/chiavi diverse: uno schema mancante in un trial non è coperto dalla presenza nell'altro. Radici assolute diverse con stessi percorsi relativi devono invece coincidere.
- [ ] Eseguire rosso/verde: `uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests/scripts/test_compare_run_shape.py --no-cov`; commit `fix: preserve trial identity when comparing evidence`.

## V1 — Collaudo Linux e integrazione fra repository

### Ambiente e risorse

Verificato in pianificazione: Multipass installato e `multipass list --format json` vuoto; host con 18 GiB RAM e 11 CPU logiche. Sono presenti anche CLI Azure, Docker, Helm e kubectl; accesso Azure e disponibilità effettiva di Docker non sono stati verificati. Nessuna VM è stata creata.

- [ ] Creare una VM dedicata `nl-review-<suffix>` Ubuntu 24.04, 6 vCPU, 8 GiB RAM, disco 50 GiB; eseguire le suite in sequenza. Usare una seconda VM da 2 vCPU/2 GiB soltanto per la raccolta SSH da due host. Prima del lancio controllare memoria/disco liberi effettivi; ridurre concorrenza o passare ad Azure se insufficienti.
  Esempio di lancio, dopo avere scelto e registrato un nome libero: `multipass launch 24.04 --name nl-review-20261110 --cpus 6 --memory 8G --disk 50G`. Non adottare una VM omonima trovata all'esecuzione.
- [ ] Preparare Docker e toolchain dai lock e dai pin CI dei repository (Helm 3.16.4 e kind 0.25.0 attuali nel workbench); registrare le versioni effettive. Python/Ansible devono soddisfare i lock: non usare per errore l'Ansible del sistema host.
- [ ] Trasferire copie dei tre checkout e il NanoFaaS CI-pinned isolato. Non montare gli originali in scrittura. Non creare Multipass annidato: il test runner Linux può pilotare target remoti o Azure per i casi che richiedono VM vere.
- [ ] Se serve Azure, usare risorse dedicate/taggate per questo collaudo, una taglia compatibile con quota/architettura accertata all'esecuzione e accesso SSH ristretto all'operatore. Eseguire le stesse prove e raccogliere la stessa evidenza; non considerare il cambio provider una validazione delle misure hardware.

### Gate automatici

- [ ] Separare il gate sulle dipendenze pubblicate dal gate sui candidati. Per quest'ultimo usare una copia sacrificabile di Nanolab: aggiornare solo lì la versione attesa della collection, preinstallare la collection candidata e le due wheel Sonata nella sua venv, registrandone commit/hash/versioni. Eseguire pytest e i tool con i binari di quella venv direttamente (`.venv/bin/python -m pytest ...`), senza un successivo `uv run` che possa ripristinare i vecchi pin. Verificare prima degli import che le versioni candidate siano quelle effettive e che `ensure_installed()` non sostituisca la collection. I comandi `uv run --locked` sotto descrivono il gate normale, da ripetere sui pin definitivi dopo il rilascio.
- [ ] **Sonata:** eseguire entrambe le suite complete e i controlli di `.github/workflows/ci.yml`, incluse wheel smoke. Prove S1/S2 senza Docker devono passare anche localmente.
- [ ] **Workbench:** eseguire i controlli CI (`ruff`, format, basedpyright, bandit, ansible-lint), build locale dell'immagine supervisor e `uv run --locked pytest -q --cov -m 'not kind'`. Eseguire poi il gate kind con almeno due nodi, includendo W2/W3 e pulizia delle release.
- [ ] **Nanolab:** eseguire la suite completa su Linux contro il NanoFaaS CI-pinned, senza escludere i due casi che su macOS dipendevano da `/proc`. Comando: `uv run --locked --all-packages --all-groups pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests --cov-config=packages/nanolab/pyproject.toml`. Eseguire anche i controlli di import, tipi, lint, pre-commit e package/wheel del gate esistente.
- [ ] Allargare `.github/workflows/ci.yml` del workbench perché il job kind esegua anche le nuove verifiche Service/loadgen; evitare una nuova suite che non venga selezionata dal comando CI. Usare timeout finiti e `finally` per il cleanup anche quando la creazione del cluster riesce ma un test fallisce.

### Prove reali mirate e accettazione

- [ ] **CPU:** nella VM Linux avviare loadgen e osservatori con subset di CPU disponibile, controllare `HostConfig.CpusetCpus` e affinità dei processi. Provocare un mismatch e verificare il rifiuto prima del carico. Questo prova la meccanica, non l'isolamento prestazionale su hardware fisico.
- [ ] **Archivi:** due host SSH, stessa istanza, dati diversi; collect concorrente e sequenziale, verifica tar/checksum e nessuna sovrascrittura. Un errore di trasferimento non deve eliminare lo storage originale.
- [ ] **Recupero VM:** su VM sacrificabile pilotata dal provider, provocare un fallimento controllato nell'archivio del collector. Confermare VM conservata/stoppata dove previsto dal provider, dati recuperabili, altri servizi rimossi e stato del run fallito. Esportare le prove prima di eliminare la VM di test.
- [ ] **Sonata reale:** acquisire un container sacrificabile dentro un composite, iniettare errore nella scrittura terminale del journal; confermare che il container non resti vivo. Test resume con configurazione modificata: rifiuto esplicito, nessun consumer riusato impropriamente.
- [ ] **Telemetria:** Prometheus/cAdvisor reali; prima run completo, poi exporter raggiungibile con discovery container volutamente non funzionante. Il secondo run deve risultare incompleto, senza ricostruire falsi campioni. Provare anche exporter fermato e registrare il fault atteso.
- [ ] **Kubernetes:** da pod client interrogare i due Service; Job k6 sul worker prescelto con input/output verificati; caso Pending fallisce entro timeout; rimozione ripetuta non lascia Job, Deployment, Service o ConfigMap del test.
- [ ] **Integrazione one-shot:** avviare la più piccola topologia supportata con carico reale e i tre candidati; verificare schedule, fine dell'ultima finestra, fault supervisionati, export, coverage, report e cleanup. Riutilizzare builder e fixture esistenti; se serve un nuovo test, collocarlo in `packages/nanolab/tests/workbench/test_integration.py`, senza creare un secondo orchestratore.
- [ ] **Accettazione del disegno originario:** eseguire un run della topologia a dieci VM con input e versione NanoFaaS congelati, su risorse adeguate (Azure o siti Multipass esistenti). Riutilizzare `scripts/one-shot-multisite-smoke.py` per il preflight, poi il workflow one-shot con carico. Confrontare schema ed evidenze con il riferimento tramite N5, eseguire report/model comparison e iniezioni exporter morto/swap con carico controllato. Il preflight senza carico non vale come accettazione. Se mancano riferimento, freeze o capacità infrastrutturale, registrare esplicitamente il gate non eseguito: non sostituirlo con una simulazione né dichiarare la migrazione qualificata.

### Artefatti, integrazione e rilascio

- [ ] Conservare sotto `docs/testing/2026-11-10-review-remediation/` riepilogo per R1–R10, commit effettivi, versioni, comandi, risultati rosso/verde, JUnit/log, manifest Kubernetes, inventari senza segreti, CPU evidence e hash degli archivi. I dati voluminosi possono stare in un run-dir esterno referenziato con hash.
- [ ] Provare una fresh install delle wheel Sonata candidate e della collection candidata in ambienti temporanei; nessun override locale deve nascondere un problema di packaging o dipendenze nel gate finale.
- [ ] Preparare release note e artefatti. Quando autorizzata la pubblicazione: rilasciare prima entrambe le distribuzioni Sonata alla stessa versione, poi collection/immagine supervisor workbench coerenti; solo dopo aggiornare tutti i pin Nanolab e `uv.lock`. Verificare assenza di `git+` e rifare lo smoke installato con artefatti pubblicati.
- [ ] Rivedere il diff finale per regressioni e duplicazioni; ogni rilievo deve avere test e prova di chiusura. Rimuovere VM/cluster/container creati per il collaudo soltanto dopo verifica delle evidenze; pulizia limitata ai nomi/ID registrati, mai purge globale.

## Criterio di completamento

I dieci difetti hanno regressioni automatiche verdi; i gate Linux/Docker/Kubernetes obbligatori passano senza skip infrastrutturali; i percorsi di fallimento conservano dati e causa originale; i tre artefatti funzionano insieme da installazione pulita. Distinguere sempre **correzioni verificate**, **release preparata/pubblicata** e **accettazione scientifica a dieci VM**: nessuna delle tre implica automaticamente le altre.

## Autoverifica del piano

- Copertura R1–R10 completa nella matrice; la variazione degli archivi ha sia produttori (W1) sia consumatori (N4).
- Callback S1 comprende Protocol, implementazione e doppi; S2 comprende entrambi i wrapper annidati e controllo Helm.
- Non basta leggere `docker inspect`, `.status.active`, una porta aperta o l'assenza di eccezioni: i test verificano rispettivamente placement effettivo, Pod avviato, Service raggiungibile, presenza dei dati obbligatori.
- Le prove prestazionali non sono inferite da VM virtualizzate; le attività cloud restano circoscritte e tracciate.
- Nessun prodotto modificato in pianificazione; il prossimo passo operativo è S1/N1, non un rilascio.
