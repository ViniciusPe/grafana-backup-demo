"""Backup de dashboards Grafana para o repositório.

Layout gerado:
    instances/<instance>/folders.json
    instances/<instance>/dashboards/<folder-slug>/<uid>__<slug>.json

Cada arquivo de dashboard preserva: uid, tags, version + metadados de pasta
no envelope. O commit posterior (git) gera o histórico/versão.
A detecção de novos/alterados/removidos é feita naturalmente via `git status`
porque arquivos órfãos são apagados em cada execução (sincronização completa).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from grafana_client import GrafanaClient, GrafanaError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backup")

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "instances.yml"
DEFAULT_OUT_DIR = ROOT / "instances"

SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = SLUG_RE.sub("-", value).strip("-")
    return value or "untitled"


def env_token(instance_name: str) -> str:
    key = "GRAFANA_TOKEN_" + instance_name.upper().replace("-", "_")
    tok = os.environ.get(key, "").strip()
    if not tok:
        raise GrafanaError(f"Variável de ambiente {key} não definida")
    return tok


def git_last_commit(repo_root: Path, rel_path: str) -> tuple[str, str]:
    """Retorna (sha curto, data ISO) do último commit que tocou o arquivo.

    Vazio quando o arquivo é novo (ainda não commitado).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1",
             "--format=%h|%ad", "--date=short", "--", rel_path],
            capture_output=True, text=True, timeout=10, check=False,
        )
        line = (out.stdout or "").strip()
        if not line:
            return ("", "")
        sha, _, date = line.partition("|")
        return (sha.strip(), date.strip())
    except Exception:
        return ("", "")


def git_historical_files(repo_root: Path, base_rel: str) -> list[str]:
    """Lista todo caminho de arquivo .json que já existiu sob base_rel (qualquer branch)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "log", "--all", "--pretty=format:",
             "--name-only", "--diff-filter=A", "--", base_rel],
            capture_output=True, text=True, timeout=20, check=False,
        )
        paths = {p.strip() for p in (out.stdout or "").splitlines() if p.strip()}
        return sorted(p for p in paths if p.endswith(".json"))
    except Exception:
        return []


def git_show_json(repo_root: Path, sha: str, rel_path: str) -> dict | None:
    """Lê o conteúdo de um arquivo num commit específico."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{sha}:{rel_path}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if out.returncode != 0 or not out.stdout:
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def write_index(inst_dir: Path, repo_root: Path, instance_name: str, current: list[dict]) -> None:
    """Gera INDEX.md cumulativo: ativos + removidos (histórico).

    `current` traz os dashboards do snapshot atual.
    Para descobrir os removidos, varre o histórico do git e ignora os que
    ainda existem em disco.
    """
    current_by_uid = {e["uid"]: e for e in current}
    current_paths = {e["rel_path"] for e in current}

    base_rel = (inst_dir.relative_to(repo_root) / "dashboards").as_posix()
    history_paths = git_historical_files(repo_root, base_rel)

    rows: list[dict] = []

    # Ativos (estado atual)
    for e in current:
        sha, date = git_last_commit(repo_root, e["rel_path"])
        rows.append({
            "title": e["title"] or "",
            "uid": e["uid"],
            "folder": e["folder"],
            "status": "ativo",
            "sha": sha,
            "date": date,
            "rel_path": e["rel_path"],
        })

    # Removidos (arquivos que existiram mas não estão no snapshot atual)
    seen_uids = set(current_by_uid.keys())
    for rel in history_paths:
        if rel in current_paths:
            continue
        sha, date = git_last_commit(repo_root, rel)
        if not sha:
            continue
        data = git_show_json(repo_root, sha, rel) or {}
        uid = data.get("uid") or Path(rel).stem
        if uid in seen_uids:
            continue  # já listado como ativo (ex.: mudou de pasta)
        seen_uids.add(uid)
        folder = (data.get("folder") or {}).get("title") or "—"
        rows.append({
            "title": data.get("title") or "(sem título)",
            "uid": uid,
            "folder": folder,
            "status": "removido",
            "sha": sha,
            "date": date,
            "rel_path": rel,
        })

    # Ordena: ativos primeiro (pasta/título), depois removidos (data desc)
    rows.sort(key=lambda r: (
        0 if r["status"] == "ativo" else 1,
        r["folder"].lower() if r["status"] == "ativo" else "",
        (r["title"] or "").lower() if r["status"] == "ativo" else "",
        "" if r["status"] == "ativo" else (9999 - 0),  # placeholder
    ))
    # remove + ordena por data desc separadamente
    ativos = [r for r in rows if r["status"] == "ativo"]
    removidos = sorted(
        [r for r in rows if r["status"] == "removido"],
        key=lambda r: r["date"], reverse=True,
    )

    total = len(ativos)
    rem = len(removidos)
    lines = [
        f"# Index — {instance_name}",
        "",
        f"- Dashboards ativos: **{total}**",
        f"- Dashboards removidos (histórico): **{rem}**",
        "",
        "Para restaurar: copie o **UID** e o **SHA** da linha desejada.",
        "Para um dashboard **removido**, o SHA é o último commit em que ele existia — use exatamente esse valor em `ref` no workflow de restore.",
        "",
        "## Ativos",
        "",
        "| Título | UID | Pasta | Último SHA | Última alteração | Arquivo |",
        "|---|---|---|---|---|---|",
    ]
    for r in ativos:
        rel = r["rel_path"].replace("\\", "/")
        sha_md = f"`{r['sha']}`" if r["sha"] else "_novo_"
        date_md = r["date"] or "—"
        title = (r["title"] or "").replace("|", "\\|")
        folder = r["folder"].replace("|", "\\|")
        lines.append(f"| {title} | `{r['uid']}` | {folder} | {sha_md} | {date_md} | [{rel}]({rel}) |")

    lines += [
        "",
        "## Removidos (histórico)",
        "",
    ]
    if not removidos:
        lines.append("_Nenhum dashboard removido até o momento._")
    else:
        lines += [
            "| Título | UID | Pasta | SHA p/ restore | Removido após | Arquivo (snapshot) |",
            "|---|---|---|---|---|---|",
        ]
        for r in removidos:
            rel = r["rel_path"].replace("\\", "/")
            title = (r["title"] or "").replace("|", "\\|")
            folder = r["folder"].replace("|", "\\|")
            lines.append(
                f"| {title} | `{r['uid']}` | {folder} | `{r['sha']}` | {r['date']} | [{rel}](../../tree/{r['sha']}/{rel}) |"
            )

    (inst_dir / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_config() -> list[dict]:
    with CONFIG.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("instances") or []


def backup_instance(inst: dict, data_dir: Path) -> tuple[int, int]:
    name = inst["name"]
    url = inst["url"]
    exclude_folders = {f.lower() for f in (inst.get("exclude_folders") or [])}
    exclude_tags = {t.lower() for t in (inst.get("exclude_tags") or [])}

    log.info("=== Instância %s (%s) ===", name, url)
    client = GrafanaClient(url, env_token(name))
    health = client.health()
    log.info("Health: %s", health)

    inst_dir = data_dir / name
    dash_dir = inst_dir / "dashboards"
    # zera para garantir detecção de remoções
    if dash_dir.exists():
        shutil.rmtree(dash_dir)
    dash_dir.mkdir(parents=True, exist_ok=True)
    inst_dir.mkdir(parents=True, exist_ok=True)

    # Pastas
    folders = client.list_folders()
    folder_map = {f["uid"]: f for f in folders}
    folders_out = sorted(
        [
            {"uid": f["uid"], "title": f["title"]}
            for f in folders
            if f["title"].lower() not in exclude_folders
        ],
        key=lambda f: f["title"].lower(),
    )
    (inst_dir / "folders.json").write_text(
        json.dumps(folders_out, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Dashboards
    found = client.search_dashboards()
    log.info("Dashboards encontrados: %d", len(found))

    saved = 0
    skipped = 0
    index_entries: list[dict] = []
    for entry in found:
        uid = entry.get("uid")
        if not uid:
            continue

        folder_title = entry.get("folderTitle") or "General"
        if folder_title.lower() in exclude_folders:
            skipped += 1
            continue

        try:
            payload = client.get_dashboard(uid)
        except GrafanaError as exc:
            log.error("Falha ao baixar %s: %s", uid, exc)
            continue

        dashboard = payload.get("dashboard") or {}
        meta = payload.get("meta") or {}

        tags = [str(t).lower() for t in (dashboard.get("tags") or [])]
        if exclude_tags.intersection(tags):
            skipped += 1
            continue

        # Limpa campos voláteis para diff estável
        dashboard.pop("id", None)
        dashboard.pop("version", None)
        dashboard.pop("iteration", None)

        folder_uid = meta.get("folderUid") or ""
        envelope = {
            "uid": uid,
            "title": dashboard.get("title"),
            "tags": dashboard.get("tags") or [],
            "folder": {
                "uid": folder_uid,
                "title": folder_title,
            },
            "dashboard": dashboard,
        }

        sub = dash_dir / slugify(folder_title)
        sub.mkdir(parents=True, exist_ok=True)
        # Nome do arquivo é APENAS o UID para que renomear o dashboard não
        # gere delete+add no git (o título fica preservado dentro do JSON).
        fname = f"{uid}.json"
        out_path = sub / fname
        out_path.write_text(
            json.dumps(envelope, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rel_path = out_path.relative_to(ROOT).as_posix()
        index_entries.append({
            "uid": uid,
            "title": dashboard.get("title") or "",
            "folder": folder_title,
            "rel_path": rel_path,
        })
        saved += 1

    write_index(inst_dir, ROOT, name, index_entries)
    log.info("Instância %s -> salvos: %d, ignorados: %d", name, saved, skipped)
    return saved, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--instance",
        action="append",
        help="Nome da instância (repetível). Padrão: todas do config.",
    )
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_OUT_DIR),
        help="Diretório raiz onde escrever os dados (default: <repo>/instances). "
             "Use para apontar para o checkout do branch env/<env>.",
    )
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    log.info("Diretório de dados: %s", data_dir)

    instances = load_config()
    if args.instance:
        wanted = set(args.instance)
        instances = [i for i in instances if i["name"] in wanted]
    if not instances:
        log.error("Nenhuma instância selecionada.")
        return 2

    total_saved = total_skipped = 0
    failures: list[str] = []
    for inst in instances:
        try:
            s, k = backup_instance(inst, data_dir)
            total_saved += s
            total_skipped += k
        except Exception as exc:  # noqa: BLE001
            log.exception("Falha no backup de %s: %s", inst["name"], exc)
            failures.append(inst["name"])

    log.info("Total salvos: %d, ignorados: %d, falhas: %s", total_saved, total_skipped, failures or "nenhuma")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
