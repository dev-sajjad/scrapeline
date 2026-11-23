# Scrape instructions

A scrape instruction is a small YAML file that says **what** you want collected from a website, in your own words. scrapeline works out **how**: it compiles the instruction into a draft source, picks or defines the dataset type the records must match, and writes a site adapter skeleton for a human (or, for meetings, the generator agent) to complete.

Nothing runs on its own. A compiled source starts as `draft` and stays there until a human has approved the dataset type, completed the adapter, reviewed the site's terms and robots.txt, and activated it.

## A minimal instruction

```yaml
name: Books catalogue
start_urls: [https://books.toscrape.com/]
collect: "Every book: title, price, stock status, star rating, product page link"
dataset: product
usage: {purpose: "Price tracking"}
```

Check it, then write the draft files:

```sh
scrapeline instruction validate my-instruction.yaml   # prints what it would become; writes nothing
scrapeline instruction apply my-instruction.yaml      # writes the draft files
```

Complete examples live in [`config/instructions/`](../config/instructions/):

| File | Dataset type | Shows |
|---|---|---|
| `books-toscrape.yaml` | `product` (built in) | narrowing to the fields a listing page shows; prices in GBP |
| `fake-python-jobs.yaml` | `job_posting` (built in) | a single page, no pagination, weekly schedule, `ymd` dates |
| `example-news.yaml` | `article` (built in) | leaving out the personal-data field `author` |

## Keys

| Key | Required | Meaning |
|---|---|---|
| `name` | yes | Human name of the source. |
| `id` | no | Source id (`a-z`, `0-9`, `-`, `_`). Default: a slug of `name`; names without Latin letters get `src-<hash>`. |
| `start_urls` | yes | 1 to 20 `http(s)` URLs on public host names. The first is the entry page; the others are kept in the source's `options.extra_start_urls`. See [Safety](#safety) for what is refused. |
| `collect` | yes | What to collect, in plain words (10 to 4000 characters). Kept for the generator agent. |
| `dataset` | no | Dataset type the records must match: an existing one (`product`, `article`, `job_posting`, `meeting`, or one in `config/datasets/`) or the name of a new one. |
| `fields` | no | Which fields to collect (existing dataset) or the fields of a new dataset type. See below. |
| `crawl.pagination` | no | `auto` (default), `none`, `next-link`, `page-number`, `cursor`, `load-more`. `auto` is compiled as `next-link`; check it when you fill the spec. |
| `crawl.follow_detail_pages` | no | `false` by default. The skeleton reads list pages only; detail pages need the generator agent or a hand-written `parse.py`. |
| `crawl.max_pages` | no | Pages per run, 1 to 10000 (default 50). |
| `crawl.render` | no | `auto` (default), `never`, `always`. `always` makes a browser-rendered (`js`) source; load-more pagination needs it. |
| `schedule` | no | `daily` (default), `hourly`, `weekly` or `manual`. A manual source is never enqueued by the scheduler; run it with `scrapeline run <id>`. |
| `documents` | no | `ignore` (default), `link-only`, `download`. |
| `locale` | no | How the site writes values: `language` (BCP 47, default `en`), `timezone` (IANA, e.g. `Europe/London`), `date_order` (`dmy`, `mdy`, `ymd`), `currency` (ISO 4217, e.g. `GBP`), `decimal_separator` (`.` or `,`). Strict by default: without `date_order`, `03/04/2026` is rejected rather than guessed; without `timezone`, times without an offset are rejected. |
| `usage` | yes | Why the data is collected: `purpose`, `license`, `allow_pii` (default `false`), `retention_days`, `terms_url`. May be `{}`, but a purpose is needed for personal data. |
| `allowed_hosts` | no | Extra hosts the crawler may fetch (e.g. an image or document host), as bare public host names such as `docs.example.com`. Start URL hosts are always allowed. |

### Fields and dataset types

A field is written either as a type (`price: money`) or in full:

```yaml
fields:
  url:    {type: url, key: true}          # key fields identify a record; key implies required
  title:  {type: text, required: true}
  price:  money
  format: {type: enum, values: [paperback, hardback]}
  author: {type: text, pii: true, description: Byline}
```

Types: `text`, `int`, `float`, `decimal`, `money`, `bool`, `url`, `date`, `datetime`, `time`, `enum`. Key fields must be `text`, `url`, `int`, `date` or `enum`.

How `dataset` and `fields` combine:

- **Existing dataset, no `fields`**: every field of the dataset is collected.
- **Existing dataset with `fields`**: only those fields are collected (types must match). The key and required fields of the dataset must be included. The rest are listed in the source as `fields_excluded`. New fields need a new dataset type or a new version.
- **New dataset name with `fields`**: the fields define a new dataset type (pack `user`, version 1) with at least one key field. It is written to `config/datasets/<name>.yaml` and must be approved by a human.
- **`fields` without `dataset`**: as above, named after the source id (`books-catalogue` becomes `books_catalogue`).
- **Neither**: a dataset type has to be proposed first (by hand for now) and approved. `apply` still writes the draft source so sample pages can be captured.

### Personal data

Some fields are marked as personal data (`pii`), such as `article.author`. Collecting one needs `usage.allow_pii: true` and a stated `usage.purpose`; otherwise the instruction is refused. The simplest choice is to leave such fields out, as `example-news.yaml` does. Public availability does not justify keeping personal data, so also set `retention_days`.

## What `apply` writes

| File | Content |
|---|---|
| `config/sources/<id>.yaml` | Draft source: `platform: custom`, the declarative adapter, `status: draft`, `owner: instruction`, the instruction text, locale, usage, pagination, and a `legal` block marked as not reviewed (`terms_checked: 1970-01-01`). |
| `config/datasets/<name>.yaml` | Only for a new dataset type. |
| `adapters/sites/<id>/spec.yaml` | Skeleton: a `TODO` record selector and one `TODO` selector per field with the transform its type needs (`money`, `date`, `absolute_url`, ...), plus a `next_page` or `render` placeholder when the crawl settings need one. In the directory name, `-` becomes `_`. |

`apply` refuses to overwrite an existing source or dataset file unless you pass `--force`. It never replaces an existing `spec.yaml`, which may already hold real selectors; delete it yourself if you want a fresh skeleton. `--sources-dir`, `--datasets-dir` and `--sites-dir` write somewhere else (useful for trying things out).

Because `-` becomes `_`, ids such as `shop-a` and `shop_a` share one adapter directory. `apply` writes nothing (even with `--force`) when another source config already uses that directory, or when the existing `spec.yaml` is for a different dataset type: the new source would otherwise run on someone else's selectors. Choose another id, or fix or delete that spec.

## From draft to active

`validate` and `apply` print these steps for the instruction at hand:

1. **Approve the dataset type** if it is new (or propose one if none was given): check field types, key fields and personal-data flags.
2. **Complete the adapter**: capture sample pages, then fill the `TODO` selectors in `spec.yaml` by hand. Add `--render` when the instruction has `crawl.render: always`; without it the samples hold the page before JavaScript has filled in the records.
   ```sh
   scrapeline capture <id>            # or: scrapeline capture <id> --render
   ```
   For the `meeting` dataset you can let the generator agent write the spec instead:
   ```sh
   scrapeline agent generate --instruction my-instruction.yaml --pages data/samples/<id>
   ```
   The agent works offline on the captured pages and the deterministic tester checks its spec. With `--apply`, a passing spec and the draft source are committed on an `agent/*` branch for a human to review and merge. The generator still starts from a Meeting skeleton, so it refuses every other dataset type (built-in or new) until it works from the dataset registry; write those specs by hand.
3. **Review the site's terms of use and robots.txt.** Record the review date in `legal.terms_checked` and why collecting is allowed in `legal.notes`. Prefer an official API when the site has one.
4. **Draft live run**: `scrapeline run <id>` (it registers the source in the database as `draft`). Check the rows and the quality checks.
5. **Activate**: `scrapeline source set-status <id> active`. From then on the scheduler runs it on its schedule (never for `manual`).

## Safety

- Instruction text is untrusted. Files are written with a YAML serialiser, so quotes or newlines in `collect` or `name` cannot add keys to the source config. Any text containing `${` is refused (`${NAME}`, `${NAME:-default}` or any later form), because source configs expand such references from the environment.
- Start URLs and `allowed_hosts` are checked before anything is written, without a DNS lookup: no user name or password in a URL, no IP addresses, and no `localhost`, single-label or private-network names (`.local`, `.internal`, `.lan`, `.corp`, ...).
- Every URL still goes through the full URL policy (DNS resolved, private addresses refused), robots.txt and the rate limit when the trusted worker fetches it (`capture`, `run`). Agents never fetch pages themselves.
- Blocked responses (403, 429, challenge pages) are detected and handled, never evaded.
