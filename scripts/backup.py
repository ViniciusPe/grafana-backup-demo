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
        (sub / fname).write_text(
            json.dumps(envelope, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        saved += 1

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
