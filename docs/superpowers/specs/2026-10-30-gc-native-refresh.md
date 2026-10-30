# Nuova campagna GC/JIT/native — specifica

Data: 2026-10-30. Stato: proposta tecnica; implementazione e campagna non eseguite.

## Obiettivo e decisioni

Ripetere sulla versione attuale di NanoFaaS lo studio estivo su GC e compilazione,
perché codice e costruzione degli artefatti sono cambiati. I vecchi risultati
servono a progettare il confronto, non vengono aggregati ai nuovi risultati.
L'utente ha richiesto pull di NanoLab e pianificazione degli aggiustamenti.

Usare sette configurazioni a composizione identica:

| Chiave runtime esistente | Modalità | Collector | Compilazione |
| --- | --- | --- | --- |
| jvm | JVM | Serial | solo C1 |
| jvm-g1 | JVM | G1 | solo C1 |
| jvm-c2 | JVM | Serial | tiering completo |
| jvm-g1-c2 | JVM | G1 | tiering completo |
| native-os | native | Serial | -Os |
| native-o3 | native | Serial | -O3 |
| native-o3-g1 | native | G1 | -O3 |

Tutte includono k8s-deployment-provider, async-queue e build-metadata. Stessa
qr-code-rust, payload e digest della funzione; una replica, concorrenza e coda
fisse, nessun autoscaling, adattamento, offload o P2P. Questo misura il runtime
nel deployment k3s, non il core privo di backend. Il pilot deve escludere che la
funzione o il generatore nascondano la saturazione del control plane.

## Approccio proposto

Estendere compare ed experiment-01 con sette recipe di campagna e selezione esplicita via
--variants. Riutilizzare calendario, ledger, receipt, raccolta e report. Le sette chiavi runtime sono già definite in VARIANTS: riusarle senza un nuovo
catalogo ControlPlaneVariant. Il contesto experiment-01 seleziona le recipe
experiment-01-runtime-<chiave>.yaml; il confronto storico mantiene le proprie
comparison-<chiave>.yaml e i propri default. Non creare un
secondo orchestratore, un nuovo comando o una campagna di ablation dei moduli.

Per tutte le nuove build native usare builder: container sulla VM misurata.
NanoFaaS seleziona già Community per Serial e Oracle per G1. Alternativa scartata:
estendere il bootstrap host Community con un installer Oracle; duplica funzioni
che la build attuale già fornisce. Riutilizzare solo il vecchio confronto a due
funzioni richiederebbe meno recipe ma non offre il protocollo a gradini e il
ledger per coorte già disponibili in experiment-01.

## Protocollo da calibrare

Pilot su 1, 2 e 4 CPU del control plane, limite container comune iniziale 512 MiB;
funzione dimensionata e mantenuta uguale fra varianti. Heap iniziale comune
64 MiB, come experiment-01: è una condizione da verificare, non una promessa
che G1 e ogni toolchain la supportino. Un limite incompatibile o un OOM resta
registrato. Prima della campagna finale si può scegliere un nuovo heap comune,
con nuove recipe, identità e pilot; mai correggere una sola variante in corsa.
Questi sono budget del control plane, distinti dai budget completi di experiment-02.

Idle, warmup, gradini crescenti e drain; nessuna GC forzata. Il pilot determina
calendario comune, warmup, concorrenza, obiettivi del generatore/servizio e numero
di ripetizioni. Con tre ripetizioni e tre CPU si avrebbero 63 celle: è una stima
di matrice, non un protocollo finale o un'autorizzazione di spesa.

## Risultati e limiti

Memoria working set, CPU, throttling, throughput, correttezza, p50/p95/p99,
rifiuti, errori, timeout e pending per coorte; avvio e costi di costruzione
separati. GC: numero e tempo da MXBean dove disponibili, dati JFR per native G1.
Assenza della metrica non significa zero; tempo di una VM operation JFR non
va presentato automaticamente come pausa GC equivalente a Micrometer.

Il contrasto native Serial/G1 cambia anche distribuzione e monitoring: lo si
riporta come confronto di configurazioni. La matrice JVM 2x2 isola GC e tiering;
C1/full tiering è la configurazione richiesta, la presenza effettiva di C2 nel
warmup richiede evidenza separata. Event loop invariati e configurazione osservata.

## Confine operativo

Ora: aggiornamento Git e documenti. In seguito: implementazione, verifiche locali,
smoke con build effettive e proposta di pilot. Nessun provisioning, campagna,
merge, push o nuova misura è autorizzato da questo documento.

## Revisione orientata al riuso

NanoFaaS possiede già la build richiesta: plugin io.github.nanofaas.build 0.1.2,
validateRecipe, publishRecipe, builder container, configurazione Spring AOT,
metadata e distribution.json. NanoLab possiede già runtime, ledger, snapshot,
receipt, interpretazione dei contatori e rendering. Gli interventi riguardano
l'integrazione di questi percorsi. Nessuna modifica NanoFaaS/plugin è prevista
senza un errore riproducibile nel contratto corrente.

Le recipe di campagna sono dati sperimentali necessari (QR, heap, builder), non
un nuovo formato: usare lo schema incluso nel plugin e build.variant con la
chiave esistente, invece di vietare una funzione nativa già usata dalle release.
La selezione è una tupla di sette chiavi esistenti, non sette nuove definizioni
Python. Il workload a gradini e la funzione QR restano quelli concordati.
