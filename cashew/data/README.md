# Data

The dataset used to build Cashew is **not included** in this repository. The original
fixtures were synthetic but derived from real design-partner statements and marked
do-not-share, so they are deliberately excluded from anything published externally.

To run the engine, drop your own data here in the layout below. Everything is local — the
mock harness reads these CSVs and replays them as TrueLayer + Xero APIs.

## Layout

```
data/
└── <dataset-name>/
    └── <Org Name>/
        ├── <Org Name> bank statement.csv
        └── <Org Name> Xero.csv
```

The active org is chosen by slug (e.g. `myco-scn-1`) via `CASHEW_ORG` in `.env` or
`GET /sim/orgs`. Org slugs are derived from the folder names.

## CSV formats

The two files per org must **reconcile 1:1** — same row count, same total.

**`<Org Name> bank statement.csv`** — the open-banking feed:

```csv
Date,Account,Description,Counterparty,Money In (GBP),Money Out (GBP),Balance (GBP),Cashew Category
```

- `Cashew Category` is the ground-truth label. The mock harness does **not** expose it on
  the open-banking endpoint (categorisation is a real problem the engine must solve); it is
  only served on `/sim/truth/labels` for scoring.

**`<Org Name> Xero.csv`** — the matching reconciled accounting ledger:

```csv
Date,Contact,Description,GL Account Code,GL Account Name,Tax Type,Money In (GBP),Money Out (GBP),Type,Reconciled
```

Amounts are GBP. Scenario months are treated as the current month; earlier months form the
baseline history the forecast learns from.
