# Chiusura del rilievo sulla salute della calibrazione

Questo seguito aggiorna il dossier iniziale, che conserva le evidenze e i limiti
osservati prima delle correzioni. Su indicazione dell'utente non viene ripetuto
alcun workflow a dieci VM: il collaudo finale è limitato a cinque VM.

## Causa e correzione

Il fault `up.node.operator=0` non era un semplice ritardo di avvio. Il dump della
serie Prometheus mostra ripetuti timeout di scrape da cinque secondi. Un
node-exporter diagnostico isolato sul server riproduce il blocco; il dump delle
goroutine lo localizza in `sysfs.parseCpufreqCpuinfo`, durante `os.ReadFile`.
Con il solo collector `cpufreq` disattivato, cinque scrape completi terminano in
0,02–0,05 secondi, conservando le metriche CPU, memoria e swap.

Workbench espone `config.cpufreq`, abilitato per default. Nanolab lo disabilita
per i propri node-exporter, perché non usa quelle serie nella calibrazione.
Il preflight hardware continua a rilevare frequenza e governor separatamente.
Non sono stati modificati kernel, sicurezza o soglie del supervisor.
Il comando condiviso serve entrambe le forme container e pod.

Prima di scrivere il profilo o il riferimento, `PublishCalibrationTask` registra
lo stato finale del supervisor e richiede `state=healthy` con un controllo già
eseguito. Fault persistente, stato degradato, assenza di controlli e supervisor
irraggiungibile impediscono la pubblicazione; i campioni raw restano disponibili.
Il fault persistente viene rifiutato anche quando i singoli target sono tornati
sani. Si riusa il supervisor esistente senza aggiungere thread di monitoraggio.

Riferimenti per il punto di blocco:
[procfs sysfs](https://github.com/prometheus/procfs/blob/master/sysfs/system_cpu.go)
e [collector cpufreq](https://github.com/prometheus/node_exporter/blob/master/collector/cpufreq_linux.go).

## Verifiche già concluse

- Test di regressione prima rossi, poi verdi: pubblicazione rifiutata su guasto;
  flag del collector verificato sia nel container reale sia nei valori Helm.
- 121 test Linux mirati superati per pipeline, profili, capacità e Workbench.
- Workbench Docker su Mac: 198 passati, coverage supervisor 98,28%; tre skip
  perché Docker Desktop non espone la rete host ai collector, tre prove kind
  escluse da questo comando e affidate alla CI Linux dedicata.
- Ruff e Ansible lint puliti; basedpyright Nanolab e Workbench senza errori.
- Run reale a tre VM `c46993d5b0e9`: 49 step, profilo pubblicato, supervisor sano
  senza anomalie, stato della Resource `destroyed`, nessuna VM residua.
- Le due VM locali di revisione sono state eliminate dopo il recupero dei log
  e la lettura integrale degli archivi trasferiti. Inventario locale vuoto.
- Le due frasi obsolete nei README Workbench sulla rete pod sono corrette.

Le evidenze del seguito sono conservate in
`/Users/micheleciavotta/Downloads/nanolab-review-artifacts-20261110/health-followup/`.
Lo snapshot e i campioni del run a tre VM sono in
`run-health-verified-evidence.tgz`, SHA256
`41cb238e30730df3708186ee33a6ecd70131d29fa5bc6511afe536080504b3af`.
Gli archivi non includono chiavi private né il piano o lo stato Terraform.

Il run usa `purpose=workflow-validation`, campionamento e durata ridotti.
Non costituisce un confronto scientifico con un freeze di riferimento.

## Suite completa e controlli di integrazione

Il nuovo run completo Linux dura 923,51 secondi: 6.619 passati, 43 skip,
20 subtest passati e un errore di packaging perché `uv` non era nel PATH del
runner. Dopo la correzione del solo PATH, entrambi i test di packaging passano.
Ventidue skip erano dovuti al percorso della collection: tutti e 22 passano
puntando alla collection già installata. Sono quindi 6.642 test distinti passati,
con 21 skip residui espliciti; non viene presentato il log iniziale come un run
interamente verde. Coverage del run completo 87,50%, soglia 86%; 111 warning.
Gli skip residui richiedono artefatti storici, ambienti di riferimento, immagini
precompilate o test opt-in. Le prove Docker installate già eseguite restano nel
dossier iniziale; il collaudo reale di questa correzione usa le wheel candidate.

Il pre-commit completo Nanolab passa tutti i controlli dopo l'installazione da
PyPI di Sonata 0.7.1: Ruff, formattazione, tipi, contratti di import, Bandit e
igiene dei file. Il lock contiene soltanto distribuzioni pubblicate per Sonata.

La CI Linux Workbench sul commit `93264af` passa 201 test Docker e tre prove
kind su due nodi, più lo smoke di installazione da git. Copertura supervisor
98,27% con Python 3.14. La differenza dal conteggio Mac corrisponde ai tre test
che richiedono la rete host Linux.

## Collaudo finale a cinque VM

Il namespace `a4582c747682` completa 69 step con exit 0: quattro nodi edge e un
cloud, ciascuno con 1 CPU, 2 GiB RAM e 8 GiB disco. Il percorso è quello reale
Nanolab → Resource Sonata → OpenTofu → provider Multipass firmato 0.1.0.
Il profilo viene pubblicato con supervisor `healthy`, 13 controlli sani e nessuna
anomalia; campioni, capacità e snapshot Prometheus sono conservati.

Il rilascio automatico conclude con stato proprietario `destroyed`; le cinque
VM sono assenti dall'inventario. Il container diagnostico cpufreq è stato rimosso.
Sul server restano soltanto la VM preesistente già Deleted e il container buildkit
dell'utente; l'inventario Multipass locale è vuoto. Nessuna risorsa Azure creata.

Archivio `run-health-five-evidence.tgz`, SHA256
`49989fa2aa8b1c4499c7cdd3f8b229788d987031785d34d5d76251abbfb0e3bc`,
verificato dopo il trasferimento. Il log completo è `one-shot-five.log`.

## Rilasci e dipendenze pubbliche

- [Sonata PR 27](https://github.com/Nanofaas/sonata/pull/27) integrata dopo CI
  verde. Il tag `v0.7.1` punta al merge `9e8491b`; engine e tasks 0.7.1 sono
  pubblicati su PyPI e installati nel workspace senza override locali.
- [Workbench PR 1](https://github.com/Nanofaas/nanofaas-workbench/pull/1)
  integrata dopo CI verde. La [release 0.2.0](https://github.com/Nanofaas/nanofaas-workbench/releases/tag/v0.2.0)
  pubblica la collection e l'immagine Linux AMD64/ARM64 dal merge `5267edd`.
  Digest dell'indice immagine:
  `sha256:f9715f701e982f68cbb73f22e5b7ff33f0f1cd9740050eaaa93f6c4b7c3ad038`.
- Nanolab aggiorna i tre pin Sonata a 0.7.1, il lock con gli hash PyPI e il pin
  Workbench a `v0.2.0`, insieme ai fixture di installazione e alla guida operatore.

Lo smoke finale sul server usa Sonata 0.7.1 reinstallato da PyPI, Workbench
installato dal tag pubblico `v0.2.0` in un workspace nuovo e l'immagine pubblica
con il digest sopra. I tre test Docker passano in 70,48 secondi: compensazione
su errore del journal, affinità e mismatch CPU, k6 reale, metriche obbligatorie,
discovery mancante, fault del supervisor e snapshot per host. Tutti i container
dello smoke sono rimossi. Il test usa `cpufreq=false`, come la piattaforma Nanolab.

La wheel Nanolab finale è costruita dal worktree e installata normalmente;
non è una nuova release Nanolab su PyPI. SHA256:
`356f3c1a0916b8d800165da2abf6aa56c82d1c34d269df1366b5984c92e41cc3`.
La provenienza degli import da site-packages, i log di installazione, JUnit e raw
sono in `public-smoke-evidence.tgz` nell'archivio permanente del seguito.
