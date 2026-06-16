"""Restore de dashboards a partir do repositório para uma instância Grafana.

Uso típico (via workflow manual):
    python restore.py --instance prod-a            # restaura tudo
    python restore.py --instance prod-a --folder "Observability"
    python restore.py --instance prod-a --uid abc123
    python restore.py --instance prod-a --ref <git-sha>   # (apenas info; checkout é feito no workflow)

Operações:
- Cria pastas ausentes preservando UID original.
- Faz import com overwrite=True, preservando uid/tags do JSON.
- Por padrão NÃO remove dashboards que existem na instância mas não no repo
  (use --prune para habilitar).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import yaml

from grafana_client import GrafanaClient, GrafanaError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("restore")

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "instances.yml"
DEFAULT_DATA_DIR = ROOT / "instances"


def env_token(instance_name: str) -> str:
    key = "GRAFANA_TOKEN_" + instance_name.upper().replace("-", "_")
    tok = os.environ.get(key, "").strip()
    if not tok:
        raise GrafanaError(f"Variável de ambiente {key} não definida")
    return tok


def instance_verify_ssl(inst: dict) -> bool | str:
    verify_ssl = inst.get("verify_ssl", True)
    ca_bundle = (inst.get("ca_bundle") or "").strip()
    if ca_bundle:
        return ca_bundle
    return verify_ssl


def load_instance(name: str) -> dict:
    with CONFIG.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    for inst in data.get("instances") or []:
        if inst["name"] == name:
            return inst
    raise SystemExit(f"Instância '{name}' não encontrada em {CONFIG}")


def iter_dashboard_files(data_dir: Path, inst_name: str, folder_filter: str | None, uid_filter: str | None):
    base = data_dir / inst_name / "dashboards"
    if not base.exists():
        raise SystemExit(f"Diretório de backup não existe: {base}")
    for path in sorted(base.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            log.error("JSON inválido %s: %s", path, exc)
            continue
        if uid_filter and data.get("uid") != uid_filter:
            continue
        if folder_filter and (data.get("folder", {}).get("title") or "").lower() != folder_filter.lower():
            continue
        yield path, data


def ensure_folder(client: GrafanaClient, folder: dict, existing: dict[str, dict]) -> str | None:
    uid = folder.get("uid")
    title = folder.get("title")
    if not uid or not title or title.lower() == "general":
        return None
    if uid in existing:
        return uid
    log.info("Criando pasta ausente uid=%s title=%s", uid, title)
    try:
        created = client.create_folder(uid, title)
        existing[uid] = created
        return uid
    except GrafanaError as exc:
        log.error("Falha criando pasta %s (%s): %s", title, uid, exc)
        return None


def restore(instance: str, folder: str | None, uid: str | None, prune: bool, dry_run: bool, message: str, data_dir: Path) -> int:
    inst = load_instance(instance)
    client = GrafanaClient(inst["url"], env_token(instance), verify_ssl=instance_verify_ssl(inst))
    log.info("Health: %s", client.health())
    log.info("Diretório de dados: %s", data_dir)

    existing_folders = {f["uid"]: f for f in client.list_folders()}

    targets = list(iter_dashboard_files(data_dir, instance, folder, uid))
    log.info("Dashboards a restaurar: %d (dry_run=%s)", len(targets), dry_run)

    applied = 0
    errors: list[str] = []
    repo_uids: set[str] = set()
    for path, data in targets:
        d_uid = data.get("uid")
        if not d_uid:
            log.warning("Ignorando %s: sem uid", path)
            continue
        repo_uids.add(d_uid)
        folder_uid = ensure_folder(client, data.get("folder") or {}, existing_folders)
        dash = data.get("dashboard") or {}
        # Garante uid no payload (Grafana usa para idempotência)
        dash["uid"] = d_uid
        dash.pop("id", None)

        if dry_run:
            log.info("[dry-run] %s -> folder=%s", d_uid, folder_uid or "General")
            continue
        try:
            client.import_dashboard(dash, folder_uid, message=message)
            applied += 1
        except GrafanaError as exc:
            log.error("Falha import %s (%s): %s", d_uid, path.name, exc)
            errors.append(d_uid)

    if prune and not uid and not folder:
        current = {e["uid"]: e for e in client.search_dashboards() if e.get("uid")}
        to_delete = [u for u in current if u not in repo_uids]
        log.info("Prune: %d dashboards a remover", len(to_delete))
        for u in to_delete:
            if dry_run:
                log.info("[dry-run] DELETE %s", u)
                continue
            try:
                client.delete_dashboard(u)
            except GrafanaError as exc:
                log.error("Falha delete %s: %s", u, exc)
                errors.append(u)

    log.info("Restore concluído: aplicados=%d erros=%d", applied, len(errors))
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--instance", required=True, help="Nome da instância (de instances.yml)")
    p.add_argument("--folder", help="Restaurar apenas dashboards desta pasta (título)")
    p.add_argument("--uid", help="Restaurar apenas o dashboard com este uid")
    p.add_argument("--prune", action="store_true", help="Remover dashboards que não existem no repo")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--message", default="Restore via GitHub Actions", help="Mensagem de versão no Grafana")
    p.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="Diretório raiz onde ler os dados (default: <repo>/instances).",
    )
    args = p.parse_args(argv)
    return restore(
        args.instance,
        args.folder,
        args.uid,
        args.prune,
        args.dry_run,
        args.message,
        Path(args.data_dir).resolve(),
    )


if __name__ == "__main__":
    sys.exit(main())
