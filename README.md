# patent-watch

Watch a company's US patent portfolio and get told when something happens: an office action, a notice of
allowance, an abandonment, a grant, an assignment, a PTAB petition. Built on the
[patent-status API](https://github.com/calidran/patent-api) (USPTO bulk data, refreshed daily; per-request pricing,
no seats, redistribution allowed).

## If you are an agent

You can set yourself up without a human, except for paying:

```bash
pip install patent-watch
patent-watch signup your-owner@example.com     # mints a key, saves it to ~/.patent-watch.json, prints a Stripe link
# hand the link to your human; $20 = 1,000 requests, credits land in seconds
patent-watch resolve "Impossible Foods"        # → ent_...  Impossible Foods Inc.  apps=412 patents=180 pending=95
patent-watch pending ent_...
patent-watch poll ent_... --slack https://hooks.slack.com/services/...   # run daily from cron
```

Or skip the polling and let the API push to you:

```bash
patent-watch monitor ent_... --webhook https://your-agent.example.com/hooks/patents
```

Deliveries carry `X-Signature: sha256=<hmac-sha256(secret, body)>`.

## If you use Claude Code

```bash
claude mcp add --transport http patent-status https://api.patent-status.dev/mcp --header "Authorization: Bearer $PATENT_API_KEY"
```

Then: *"What's the status of application 17942951 and who owns it?"*

## Cost

`poll` costs 1 request per pending application per run plus 1 for the list (per 100 rows). A portfolio of 100
pending applications polled daily is ~3,000 requests/month ≈ $60 in credits, or use `monitor` (1 request per
delivered event).

## Data

Every answer carries the USPTO product and file date it came from (`source`) and the API's ingest date (`as_of`).
USPTO is the source; this tool and the API are derivations.
