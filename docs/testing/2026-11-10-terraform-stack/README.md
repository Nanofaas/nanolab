# Terraform/OpenTofu: implementazione e collaudo del 10 novembre 2026

Il task generico vive in `sonata-tasks`; Nanolab configura e consuma una flotta
Multipass per sito. Il task non conosce Nanolab, nomi dei nodi o dettagli Multipass.
La flotta possiede lo stato e distrugge le VM; i nodi prendono in prestito le VM
per bootstrap, carico, raccolta e cleanup.

## Contratto consegnato

- `TerraformSpec`, `TerraformTask` per init/plan/apply/output/destroy e
  `terraform_stack_resource`, usando gli executor e le Resource esistenti.
- Input immutabili e fingerprint, provider lock, applicazione del piano salvato,
  timeout finiti, lock dello stato e parallelismo esplicito (default 1).
- Resource proprietaria limitata a stato locale e workspace default: rifiuta
  infrastruttura preesistente, backend alternativi, stato estraneo e drift al resume.
- Piani, stato e diagnostica privati; soltanto output selezionati e non sensibili
  escono dal task. CLI config e variabili Terraform devono essere dichiarate.
- Un apply incerto richiede recupero esplicito. Una raccolta non verificata conserva
  l'intera flotta, anche dopo ricostruzione del processo.
- In Nanolab `provisioning: opentofu` attiva il percorso; `direct` resta il default.
  CPU, RAM, disco, nomi, output e chiave SSH sono verificati prima del bootstrap.

Vedere [la guida operatore](../../one-shot-workflows.md) e l'ambiente distribuito
`multipass-opentofu.yaml.example`. I sorgenti sono su `codex/review-remediation`:
Sonata `/private/tmp/nl-review-sonata`, Nanolab nel worktree Codex `c5a7`, Workbench
`/private/tmp/nl-review-workbench`. Nessun push, merge o pubblicazione.

## Versioni e candidati

| Componente | Candidato verificato |
| --- | --- |
| Sonata engine/tasks | 0.7.1, commit `0adbdb3` (correzioni finali `676b41c`) |
| Nanolab | 0.6.0, feature `5e1e4480`, percorsi `60896158`, revival/SSH `16446acb` |
| Workbench | 0.2.0, commit `7f895d3` |
| Multipass provider | release firmata 0.1.0, darwin_arm64 e linux_arm64 |
| OpenTofu | 1.13.8 macOS, 1.13.9 Linux |
| Server / guest | Ubuntu 24.04 ARM64; Multipass 1.17.0 |
| Toolchain | Python 3.12 Linux; Java 25; Helm 3.16.4; kind 0.25.0 |
| Ansible | core 2.20.0; community.docker 4.8.3; kubernetes.core 6.2.0 |
| NanoFaaS | CI pin `401391a0d39f84364570a17044dc4d8b0db23596`, copia isolata |

Firma provider verificata con fingerprint
`BC1D2E34BCE6C30673A5CAA4FE32DA979EF37C37`, checksum verificati prima di preparare
il mirror locale. Il provider non è stato modificato.

I pin pubblici di Nanolab restano Sonata 0.7.0 e Workbench 0.1.1 finché i candidati
non saranno pubblicati. Le copie di collaudo sostituiscono solo quei pin con
0.7.1/0.2.0; anche i fixture che asseriscono letteralmente la vecchia versione
sono adattati nella copia di test. Le wheel candidate **non sono** un rilascio
pubblico già installabile dai normali pin. Gli hash sono nel manifest esterno.

## Prove eseguite

| Gate | Evidenza / esito |
| --- | --- |
| Sonata engine Linux | 266 passati, coverage 96.99% |
| Sonata catalogo Linux | 856 passati, 2 skip per CLI Terraform assente; coverage 92.95% |
| Terraform mirati | 44 passati, 2 skip Terraform; OpenTofu reale incluso |
| Nanolab completo Linux | 6636 passati, 22 skip, 20 subtest; coverage 87.49% (soglia 86%) dopo la correzione dei percorsi |
| Workbench Docker, dipendenze esatte | 198 passati, coverage 98.28% |
| Workbench kind, due nodi | 3 passati: Service da pod client, loadgen sul nodo corretto, Pending/fallimento e cleanup |
| Wheel installate, Docker reale | 3 passati anche dopo la correzione revival/SSH; 70.27 s |
| CLI NanoFaaS reale | `cli-contract-container`: 24 step completati, invocazione/update/delete e cleanup |
| Profili recipe | validazione Gradle dei profili distribuiti superata |
| Statici | lint, format, tipi, contratti import, bandit, pre-commit; tipi finali Nanolab 0 errori |

Il run completo è successivo a `60896158`, dura 1019.09 s ed esce con codice 0.
La successiva correzione revival/SSH `16446acb` è coperta da **183 test Linux
passati**, tre skip opt-in eseguiti separatamente come tre prove Docker reali,
e dal workflow reale a 49 step. Non è stata ripetuta un'altra suite completa
dopo queste due modifiche mirate.

I 22 skip sono espliciti: 3 test installati opt-in (passati separatamente),
4 con artefatti storici, 3 con ambiente Python di riferimento, 2 SciPy,
4 k6 CLI/loopback, 2 sorgenti runtime/native, 1 immagini precompilate e
3 contratti specifici JVM/ELF. L'elenco esatto è `nanolab-final-skips.json`
nell'archivio runner; gli skip non sono stati conteggiati come test riusciti.
Le 111 warning della suite rimangono nel log.

La wheel Nanolab finale ha SHA256
`732d0690ae11bcadfde41486b9e61cecfab08ebcc9957963ede5f847bad9547a`.
Il confronto byte per byte con i sorgenti di `e2d5b04b` trova soltanto la
sostituzione dichiarata del pin Workbench; i metadati richiedono Sonata 0.7.1.
`installed-provenance-final.json` conferma gli import da site-packages.

Le tre prove delle wheel installate includono errore del journal dopo acquire
con cleanup di un container reale; k6 e osservatori con cpuset e affinità del
processo verificati; mismatch reale rifiutato; Prometheus/cAdvisor con coverage
normale e discovery container mancante nonostante `up=1`; exporter fermato e
fault del supervisor; snapshot raccolto nel percorso qualificato per host.

La flotta di collaudo è stata creata passando dal vero builder Nanolab alla
Resource Sonata e al provider firmato: tre VM, 2+1+1 vCPU, 8+2+2 GiB RAM,
32+8+8 GiB disco. Comandi guest e trasferimenti con contenuti/checksum distinti
verificati. La stessa flotta ospita il runner Linux; non è una prova prestazionale.

Apply parziale reale: una VM creata, poi `local-exec` con exit 19. Il cleanup ha
incontrato un reset SSH e lo stack è rimasto `uncertain`; riacquisizione automatica
rifiutata, destroy esplicito successivo riuscito e inventario/stato vuoti verificati.
Il test bridge Docker riproduce il drift con tutti gli IPv4, poi verifica piano
senza cambiamenti e stessa VM esportando soltanto l'indirizzo primario.

Due guest SSH con la stessa istanza hanno prodotto tar diversi sia in parallelo
sia in sequenza; un errore di fetch ha lasciato i dati sorgente intatti. La prova
collector con destinazione `/dev/full` ha conservato la VM e verificato l'hash
recuperato dopo il riavvio (dossier remediation).

Il workflow reale `one-shot-calibration` sul server operatore ha completato
**49 step, exit 0**: creazione di tre VM (1 CPU/2 GiB/8 GiB ciascuna), bootstrap,
runtime, workload NanoFaaS reale, 12 campioni di occupazione fisica, prove di
capacità, profilo immutabile `purpose=workflow-validation`, snapshot Prometheus
e teardown. Namespace `216c5f98dff1`, stato finale `destroyed`, VM assenti.

**Rilievo aperto emerso dal run:** `workbench/supervisor-status.json` conserva
`state=fault` per uno scrape `up.node.operator=0`, anche se tutti i target sono
sani alla lettura finale. `build_one_shot_calibration_plan` non collega
`CampaignSupervision` e ha pubblicato il profilo nonostante il fault. Quindi
questo è un collaudo funzionale di provisioning/carico/cleanup, **non** un gate
scientifico o una verifica completa della salute del workflow. Il fault e lo
snapshot sono conservati in `evidence/one-shot-evidence.tgz`. La causa dello
scrape transitorio e la politica bloccante della calibrazione richiedono un
intervento separato; non è stata aumentata la tolleranza per nascondere il fault.

## Revisione finale e correzioni

La [revisione indipendente](final-review.md) ha trovato sei problemi importanti,
nessuno critico. Tutti corretti con test prima rossi e poi verdi.

| Rilievo | Correzione |
| --- | --- |
| R1: input effettivi non congelati | rifiuto di TF_VAR/CLI config impliciti; bytes della config esplicita inclusi nell'identità |
| R2: compensation pubblicava destroyed prima di uncertain | il cleanup di apply fallito termina direttamente uncertain; fallimento della scrittura resta bloccante |
| R3: errore della guardia non durabile | checkpoint conservativo prima della guardia e obbligo di mantenerla alla ricostruzione |
| R4: bridge Docker causava drift | output limitato all'IPv4 primario effettivamente consumato |
| R5: chiave privata con punti | `.pub` aggiunto al nome completo |
| R6: freeze ignorava asset Terraform | `.tf` e `.hcl` inclusi nel fingerprint del pacchetto |

Il collaudo reale ha poi trovato tre difetti aggiuntivi: run-dir relativo passato
ad Ansible/Docker, binding VM senza revival necessario al cleanup Workbench,
connessione SSH persistente con appartenenza al gruppo Docker precedente al
bootstrap. Correzioni rispettivamente `60896158` e `16446acb`, con test rossi/verdi.

## Evidenze e riproducibilità

Directory permanente dell'operatore:
`/Users/micheleciavotta/Downloads/nanolab-review-artifacts-20261110/`.
Contiene `candidates/`, `evidence/runner-evidence.tgz`, log locali selezionati,
JUnit, prove raw dei test, dossier e manifest SHA256. Nessuna chiave privata
è inclusa. I file grandi non sono aggiunti al repository.

Comandi di gate (venv candidate già preparate, nessun `uv run` che ripristini pin):

```sh
# Sonata, dalla sua copia isolata
.venv/bin/python -m pytest -c pyproject.toml tests
.venv/bin/python -m pytest -c packages/sonata-tasks/pyproject.toml packages/sonata-tasks/tests
# Nanolab, dalla copia isolata e con NANOFAAS_ROOT al checkout CI-pinned
.venv/bin/python -m pytest -c packages/nanolab/pyproject.toml packages/nanolab/tests --cov-config=packages/nanolab/pyproject.toml
# Workbench
.venv/bin/python -m pytest -m 'not kind' --cov
.venv/bin/python -m pytest -m kind tests/integration/test_pod_form.py
# Test installati separati, senza conftest che isola NANOLAB_WORKSPACE
NANOLAB_WORKBENCH_INTEGRATION=1 NANOLAB_WORKSPACE=/path/to/candidate-workspace python -m pytest test_integration.py
# Workflow reale sul server operatore isolato
nanolab run scenario.yaml --environment environment.yaml --run-dir run-functional-session-fixed
```

Per ripetere la prova non riusare una directory immutabile di un run terminato:
creare un nuovo run-dir e conservare la precedente evidenza. Diagnostica e stato
privati rimangono nella directory del relativo stack.

## Limiti e decisioni esplicite

- Nessun run scientifico a dieci VM: mancano freeze/riferimento compatibili e
  disco sufficiente per quella topologia. Il successo della flotta minima non
  costituisce accettazione del disegno sperimentale.
- CLI Terraform non installata: test argv per entrambi i nomi, esecuzione reale
  con OpenTofu. Nessun test di provider Azure/spot per questa nuova integrazione.
- Backend remoti non supportati dalla Resource proprietaria; test reale negativo
  del backend locale personalizzato, senza mutare servizi di stato esterni.
- Parallelismo default 1 per la race Multipass 1.17 ARM documentata dal provider;
  nessuna modifica alla sicurezza del server. Reset SSH transitori osservati;
  stato incerto conservato e recupero esplicito verificato.
- Due VM locali di revisione sono conservate perché Multipass exec/transfer non
  le raggiunge e alcuni log non sono stati esportati. Nessuna cancellazione.
- Minore rinviato: due README Workbench conservano una frase obsoleta sulla rete
  host della forma pod, accanto alla nuova descrizione corretta; aggiornare prima
  della pubblicazione della documentazione.
- Le decisioni complete, incluse correzioni dei runner e separazione dei pin
  pubblici/candidati, sono in [execution-ledger.md](execution-ledger.md).

## Cleanup verificato

Il runner è stato rilasciato **dopo** l'esportazione verificata delle evidenze.
Il destroy automatico e il primo recupero hanno incontrato reset SSH: stato
`uncertain` conservato. Il recupero esplicito seriale, con un wrapper temporaneo
che riusa la connessione SSH già verificata, ha distrutto le tre VM; stato
Terraform vuoto e inventario del server verificati. Nessuna modifica a input
congelati, provider o sicurezza SSH. Il record storico `uncertain` non è stato
riscritto per simulare un cleanup automatico riuscito.

Sul `.35` non rimangono VM o container di test attivi. La VM preesistente
`nl-swap-diagnostic-20261012` (già Deleted) e il container buildkit dell'utente
sono rimasti invariati. Le due VM locali non raggiungibili restano conservate.
Copie operatore/cache sul server e worktree rimangono disponibili per riprodurre
il collaudo; nessun ambiente Azure è stato creato.

L'archivio finale del runner ha SHA256
`eb9b914830ef2cfca1d6a636cd6c04fea9063f066f9679a29f68215dcc6b53fa`,
uguale prima e dopo il trasferimento. È stato letto integralmente e controllato
per la presenza di JUnit finali, provenienza e prove delle wheel prima del destroy.
