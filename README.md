# grafana-dashboards-backup

Backup e restore versionados de dashboards de **3 instâncias Grafana (dev / hml / prd)**
usando **GitHub Actions**, com **um branch por ambiente**.

## Modelo de branches

```
ops/grafana-backup    ← DEFAULT BRANCH: scripts, workflows, config (sem dashboards)
├── env/dev           ← apenas dashboards do Grafana de dev
├── env/hml           ← apenas dashboards do Grafana de hml
└── env/prd           ← apenas dashboards do Grafana de prd
```

> **Por que `ops/grafana-backup` e não `main`?** Política do Enterprise proíbe
> workflows em `main`. Como `schedule` do GitHub Actions só dispara da branch
> padrão, a default branch deste repo foi movida para `ops/grafana-backup`.

- **Tudo que é código vive em `ops/grafana-backup`.** Bug fix? PR para essa branch e pronto — as branches de env *não* precisam ser atualizadas.
- **Cada `env/*` contém só os dashboards do seu Grafana**, em `instances/<env>/`.
  Histórico, PRs, CODEOWNERS e branch protection são **por ambiente**.
- O workflow agendado vive em `ops/grafana-backup` e roda em **matrix** (3 jobs paralelos),
  cada um faz checkout do seu branch `env/<env>` e dá push apenas nele.

## Como atende a cada critério

| Critério                                  | Atendimento |
|-------------------------------------------|------------|
| Pipeline automatizado                     | `backup.yml` em `ops/grafana-backup` com `schedule` horário |
| 513 dashboards exportados                 | `/api/search?type=dash-db` paginado |
| Grava no GitHub                           | `git push` para `env/<env>` na própria action |
| Histórico de versões                      | `git log` no branch do ambiente |
| Novos / alterados / removidos             | `git diff --cached --name-status` |
| API oficial                               | endpoints `/api/folders`, `/api/search`, `/api/dashboards/uid/*` |
| Service account                           | 1 token por env em secrets `GRAFANA_TOKEN_DEV/HML/PRD` |
| Preserva uid, pasta, tags                 | Envelope JSON + `folderUid` no restore |
| Atualização incremental + versionamento   | Sync completa + commit só com diff |
| Periodicidade                             | `cron: "0 * * * *"` (ajustável) |
| Sem execução manual                       | Agendado; dispatch é opcional |
| Todo dashboard de prod entra na rotina    | `/api/search` lista tudo; exclusões opt-in |
| Rastreio ao longo do tempo                | `git log env/prd -- instances/prd/...` |

## Layout em cada branch `env/<env>`

```
instances/
  <env>/                       # dev, hml ou prd
    folders.json
    dashboards/
      observability/
        abc123__cpu-overview.json
      infra/
        def456__network.json
```

Envelope de cada dashboard:

```json
{
  "uid": "abc123",
  "title": "CPU Overview",
  "tags": ["infra", "cpu"],
  "folder": { "uid": "obs", "title": "Observability" },
  "dashboard": { ... payload completo do Grafana ... }
}
```

Campos voláteis (`id`, `version`, `iteration`) são removidos para diff estável.

---

## Setup

> **Importante**: a branch padrão deste repo é `ops/grafana-backup` (não `main`).
> No GitHub Enterprise: **Settings → Branches → Default branch** → trocar para `ops/grafana-backup`
> **antes** de tentar disparar workflows agendados.

### 1. Service Accounts no Grafana
Em **cada** Grafana (dev/hml/prd):
1. **Administration → Service accounts → Add** (role **Admin** se for usar restore; **Viewer** basta para só backup).
2. **Add token** → copie o valor (`glsa_...`).

### 2. Secrets no GitHub
**Settings → Secrets and variables → Actions**:
- `GRAFANA_TOKEN_DEV`
- `GRAFANA_TOKEN_HML`
- `GRAFANA_TOKEN_PRD`

### 3. URLs em `config/instances.yml`
Edite [config/instances.yml](config/instances.yml) com as URLs reais e faça commit em `ops/grafana-backup`.

### 4. Permissões do workflow
**Settings → Actions → General → Workflow permissions** → **Read and write permissions**.

### 5. Criar os 3 branches de ambiente (bootstrap)
A partir do `ops/grafana-backup`, crie cada branch (inicialmente idêntico):

```powershell
git checkout ops/grafana-backup
git pull
git checkout -b env/dev ; git push -u origin env/dev
git checkout ops/grafana-backup
git checkout -b env/hml ; git push -u origin env/hml
git checkout ops/grafana-backup
git checkout -b env/prd ; git push -u origin env/prd
git checkout ops/grafana-backup
```

> O **primeiro** backup de cada ambiente vai popular `instances/<env>/` no
> branch correspondente.

### 6. Environments com aprovação (recomendado, só p/ restore)
Para cada env crie um environment com required reviewers em
**Settings → Environments → New environment**:
- `grafana-restore-dev`
- `grafana-restore-hml`
- `grafana-restore-prd` *(este, no mínimo, com reviewers obrigatórios)*

O `restore.yml` já referencia `environment: grafana-restore-${{ inputs.env }}`.

### 7. Branch protection (opcional)
Em **Settings → Branches** para `env/prd`: exigir PR para mudanças manuais,
**mas permitir bypass para `github-actions[bot]`** (senão o cron não consegue dar push).

---

## Operação

### Backup
- **Automático**: cron horária em `ops/grafana-backup`. Roda 3 jobs em paralelo, um por env.
- **Manual**: **Actions → Backup Grafana Dashboards → Run workflow**
  - `env`: `all` (padrão), `dev`, `hml` ou `prd`.

### Restore
**Actions → Restore Grafana Dashboards → Run workflow**:

| Campo    | Uso |
|----------|-----|
| `env`    | Ambiente de destino (dev/hml/prd) |
| `ref`    | Commit/tag a restaurar; vazio = HEAD do branch `env/<env>` |
| `folder` | Restaurar só uma pasta (vazio = todas) |
| `uid`    | Restaurar só um dashboard |
| `prune`  | Apagar do Grafana o que não está no repo (cuidado) |
| `dry_run`| **Default `true`** — sempre teste primeiro |

> **Sempre rode `dry_run=true` primeiro**, valide o log, depois rode com `dry_run=false`.

### Cenário típico — dashboard quebrado em prd
1. No GitHub, navegue até `env/prd` → abra o arquivo do dashboard → **History** → copie o SHA anterior à mudança ruim.
2. **Actions → Restore** → `env=prd`, `uid=<uid>`, `ref=<sha>`, `dry_run=true` → aprovar.
3. Confirme o log → repita com `dry_run=false`.

### Cenário típico — promover dashboard de hml para prd
Como cada env é um branch isolado, "promoção" = copiar o arquivo entre branches via PR:

```powershell
git fetch origin
git checkout -b promote/cpu-overview origin/env/prd
git checkout origin/env/hml -- instances/hml/dashboards/observability/abc123__cpu.json
git mv instances/hml/dashboards/observability/abc123__cpu.json instances/prd/dashboards/observability/abc123__cpu.json
# ajuste o campo "folder" e datasources se necessário
git add -A && git commit -m "promote: CPU Overview hml→prd"
git push -u origin promote/cpu-overview
# abra PR contra env/prd
```

Após merge, dispare o restore manual de prd.

---

## Execução local (debug)

```powershell
cd grafana-dashboards-backup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r scripts/requirements.txt

$env:GRAFANA_TOKEN_DEV = "glsa_xxx"
# escreve em ./instances/dev/
python scripts/backup.py --instance dev

# restore lê do mesmo diretório por default
python scripts/restore.py --instance dev --dry-run
```

Para apontar para outro diretório (como o workflow faz):

```powershell
python scripts/backup.py --instance dev --data-dir C:\tmp\data\instances
```

---

## Atualizando scripts/workflows

Tudo vive em `ops/grafana-backup`. Os branches `env/*` **não** precisam receber as mudanças
— o workflow sempre faz checkout dos scripts de `ops/grafana-backup` e dos dados de `env/<env>`.

---

## Mitigação de riscos

| Risco                            | Mitigação |
|----------------------------------|-----------|
| Falha de autenticação            | Health-check inicial; retries com backoff; `fail-fast: false` no matrix |
| Falha de pipeline                | Notificações de falha em Actions; `concurrency` evita duplicatas |
| Dashboard fora da rotina         | `search?type=dash-db` cobre tudo; exclusões opt-in |
| Restore destrutivo               | `dry_run=true` por default; `prune` opt-in; environment com approval |
| Drift entre ambientes            | `git diff env/hml..env/prd -- instances/` |
| Force-push acidental em env/*    | Sem `--force` em nenhum workflow |

## Limitações conhecidas

1. **Janela de perda = intervalo do cron.** Reduza o cron se necessário.
2. **Cobertura**: apenas dashboards e pastas. Alertas, datasources, usuários, plugins **não** estão no escopo.
3. **Datasource UID precisa existir no destino** no restore — mantenha datasources alinhados via provisioning.
4. **Branch protection em `env/*`** precisa permitir push do `github-actions[bot]`.
