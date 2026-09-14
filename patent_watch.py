"""
patent-watch — a small agent over the patent-status API (https://github.com/calidran/patent-api).

    patent-watch signup you@example.com            # gets a key; prints the Stripe link to fund it ($20 = 1,000 requests)
    patent-watch resolve "Impossible Foods"         # company name -> entity id + counts
    patent-watch pending ent_...                    # the entity's pending applications
    patent-watch poll ent_... --slack $WEBHOOK      # diff against last run; notify on new events (run from cron)
    patent-watch monitor ent_... --webhook URL      # server-side monitor: the API pushes signed events to your URL

Config: PATENT_API_KEY (or ~/.patent-watch.json written by `signup`), PATENT_API_URL (default https://api.patentdata.ai).
State for `poll` lives in ~/.patent-watch-state.json (last seen transaction per application).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

import click
import httpx

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    VERSION = _pkg_version("patent-watch")
except PackageNotFoundError:   # running the file directly, not the installed package
    VERSION = "dev"
# how the API tells patent-watch signups and requests apart from other agents (orgs.signup_channel = patent_watch)
HEADERS = {"User-Agent": f"patent-watch/{VERSION}"}

CONFIG = Path.home() / ".patent-watch.json"
STATE = Path.home() / ".patent-watch-state.json"
EVENTS = ("office_action", "allowance", "abandonment", "grant", "publication", "assignment", "ptab_petition")


def base_url() -> str:
    return os.environ.get("PATENT_API_URL", "").strip() or _config().get("url") or "https://api.patentdata.ai"


def _config() -> dict:
    return json.loads(CONFIG.read_text()) if CONFIG.exists() else {}


def api_key() -> str:
    key = os.environ.get("PATENT_API_KEY", "").strip() or _config().get("api_key")
    if not key:
        raise click.ClickException("no API key: run `patent-watch signup <owner-email>` or set PATENT_API_KEY")
    return key


def client() -> httpx.Client:
    return httpx.Client(base_url=base_url(), headers={**HEADERS, "X-API-KEY": api_key()}, timeout=30)


def get(c: httpx.Client, path: str, **params) -> dict:
    r = c.get(path, params={k: v for k, v in params.items() if v is not None})
    if r.status_code == 402:
        body = r.json()
        raise click.ClickException(f"key not funded: {body['checkout']} (min ${body['minimum_usd']}); run `patent-watch fund`")
    if r.status_code >= 400:
        raise click.ClickException(f"{r.status_code} {r.text[:300]}")
    return r.json()


@click.group(help=__doc__)
def cli() -> None:
    pass


@cli.command(help="Create an API key for this agent under your (human) email. Prints the funding link.")
@click.argument("owner_email")
@click.option("--agent-name", default="patent-watch")
@click.option("--url", default=None, help="API base URL")
def signup(owner_email: str, agent_name: str, url: str | None) -> None:
    u = url or base_url()
    r = httpx.post(f"{u}/agent/signup", json={"owner_email": owner_email, "agent_name": agent_name}, headers=HEADERS, timeout=30)
    if r.status_code >= 400:
        raise click.ClickException(f"{r.status_code} {r.text[:300]}")
    data = r.json()
    CONFIG.write_text(json.dumps({"api_key": data["api_key"], "org_id": data["org_id"], "url": u}, indent=1))
    CONFIG.chmod(0o600)
    click.echo(f"saved key to {CONFIG} (org {data['org_id']})")
    fund_link(u, data["api_key"])


def fund_link(u: str, key: str, amount: int = 20) -> None:
    r = httpx.post(f"{u}/agent/checkout-link", json={"amount_usd": amount}, headers={**HEADERS, "X-API-KEY": key}, timeout=30)
    if r.status_code == 503:
        click.echo("billing is not configured on this server yet; ask the operator for credits")
        return
    r.raise_for_status()
    click.echo(f"fund it here (${amount} = {amount * 50} requests, no expiry): {r.json()['url']}")


@cli.command(help="Print a Stripe link to add credits.")
@click.option("--amount", default=20, type=int)
def fund(amount: int) -> None:
    fund_link(base_url(), api_key(), amount)


@cli.command(help="Resolve a company or inventor name to entity ids.")
@click.argument("name")
@click.option("--type", "type_", type=click.Choice(["assignee", "inventor"]), default=None)
def resolve(name: str, type_: str | None) -> None:
    with client() as c:
        for e in get(c, "/v1/entities/search", q=name, type=type_, limit=10)["entities"]:
            cnt = e["counts"]
            click.echo(f"{e['entity_id']}  {e['canonical_name']:50s} apps={cnt['applications']} patents={cnt['patents']} pending={cnt['pending']}  ({e['source']})")


@cli.command(help="List an entity's pending applications.")
@click.argument("entity_id")
def pending(entity_id: str) -> None:
    with client() as c:
        for a in _all_pending(c, entity_id):
            click.echo(f"{a['application_number']}  {a['filing_date']}  {a['status']['description'][:45]:45s}  {a['title'][:60]}")


def _all_pending(c: httpx.Client, entity_id: str) -> list[dict]:
    out, cursor = [], None
    while True:
        page = get(c, f"/v1/entities/{entity_id}/applications", status="pending", limit=100, cursor=cursor)
        out += page["applications"]
        cursor = page.get("next_cursor")
        if not cursor:
            return out


@cli.command(help="Diff each pending application's timeline against the last run and notify on new events. Cron-friendly.")
@click.argument("entity_id")
@click.option("--slack", "slack_webhook", default=None, help="Slack incoming-webhook URL")
@click.option("--since-days", default=30, help="On first run, look back this many days")
def poll(entity_id: str, slack_webhook: str | None, since_days: int) -> None:
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    seen: dict[str, str] = state.setdefault(entity_id, {})
    lines = []
    with client() as c:
        for a in _all_pending(c, entity_id):
            n = a["application_number"]
            since = seen.get(n) or (date.fromordinal(date.today().toordinal() - since_days)).isoformat()
            tl = get(c, f"/v1/applications/{n}/transactions", since=since)
            new = [t for t in tl["transactions"] if t["event_type"] in EVENTS and (t["date"] > since or n not in seen)]
            for t in sorted(new, key=lambda t: t["date"]):
                lines.append(f"{t['date']}  {t['event_type']:14s} {n}  {a['title'][:50]}  ({t['code']}: {t['description']})")
            if tl["transactions"]:
                seen[n] = max(t["date"] for t in tl["transactions"])
            elif n not in seen:
                seen[n] = since
    STATE.write_text(json.dumps(state, indent=1))
    if not lines:
        click.echo("no new events")
        return
    text = "\n".join(lines)
    click.echo(text)
    if slack_webhook:
        httpx.post(slack_webhook, json={"text": f"patent-watch {entity_id}:\n```{text}```"}, timeout=30).raise_for_status()


@cli.command(help="Create a server-side monitor: the API POSTs signed events to your webhook the night they appear.")
@click.argument("entity_id")
@click.option("--webhook", required=True)
@click.option("--events", default=",".join(EVENTS), show_default=True)
def monitor(entity_id: str, webhook: str, events: str) -> None:
    with client() as c:
        r = c.post("/v1/monitors", json={"targets": {"entity_ids": [entity_id]}, "events": events.split(","), "webhook_url": webhook})
        if r.status_code >= 400:
            raise click.ClickException(f"{r.status_code} {r.text[:300]}")
        m = r.json()
        click.echo(f"monitor {m['monitor_id']} created; verify X-Signature with secret: {m['secret']}")


if __name__ == "__main__":
    cli()
