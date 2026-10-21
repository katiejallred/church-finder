# Church Finder

Finds the best email contact at US churches, preferring the **communications
director**. If a church has no comms person listed, it falls back to the
**church administrator / office**, then the **pastor**.

It works in two steps:

1. **Discover churches.** Lists churches that have a website, from
   [Overture Maps](https://overturemaps.org) (free, open data built from
   Meta's and Microsoft's business listings; finds most churches) and
   OpenStreetMap's free Overpass API. Neither needs an API key or costs
   anything. You can also skip this and use your own CSV of churches.
2. **Scrape contacts.** Visits each church website's staff, team, leadership,
   about and contact pages, pulls every email address, and ranks them by
   the job title listed next to each one.

## Setup

```bash
pip install -r requirements.txt
```

Needs Python 3.9+.

## Usage

```bash
# Everything for one city
python -m church_finder run --state AL --city Birmingham -o birmingham_contacts.csv

# A whole state, or several
python -m church_finder run --state AL -o alabama_contacts.csv
python -m church_finder run --state "AL,GA,TN" -o southeast_contacts.csv

# The whole USA (takes a long time; it can be stopped and resumed, see below)
python -m church_finder run --state all -o usa_contacts.csv

# Only build the church list
python -m church_finder discover --state TX -o texas_churches.csv

# Use only one of the two sources (the default, all, merges both)
python -m church_finder discover --state TX --source overture -o texas_churches.csv
python -m church_finder run --state AL --city Birmingham --source osm -o birmingham_contacts.csv

# Scrape your own list (any CSV with a website/URL column)
python -m church_finder scrape -i my_churches.csv -o contacts.csv

# Try a single church
python -m church_finder scrape --url https://www.briarwood.org -o test.csv
```

Useful options:

| Option | Default | What it does |
| --- | --- | --- |
| `--source all\|overture\|osm` | all | Where to find churches: Overture Maps, OpenStreetMap, or both merged |
| `--limit N` | all | Only process the first N churches (good for a trial run) |
| `--workers N` | 8 | Churches crawled at the same time |
| `--max-pages N` | 8 | Max pages fetched per church |
| `--delay S` | 1.0 | Seconds between requests to the same website |
| `--site-timeout S` | 90 | Give up on a church website after this many seconds |
| `--no-resume` | off | Start the output file over instead of skipping finished churches |
| `-v` | off | Verbose logging |

**Resuming:** results are written as each church finishes. If a run stops,
run the same command again and it skips churches already in the output file.

### Where the churches come from

**Overture Maps** publishes a free, openly licensed snapshot of tens of
millions of places every month, built mostly from Meta's (Facebook Pages) and
Microsoft's business listings. Because nearly every church has a Facebook
Page, it lists far more churches than OpenStreetMap: about 600 with a usable
website in Birmingham and over 40,000 in Texas. There is no API and no key. The
data is a set of Parquet files on a public S3 bucket, and the tool uses
[DuckDB](https://duckdb.org) to read only the rows inside the state's
bounding box straight from there: a small state takes a few seconds, a big
one about a minute, and nothing is downloaded to disk. It takes churches
(Overture's `christian_place_of_worship` branch, which also yields the
`denomination` column) and religious organizations (ministries, dioceses), and
skips closed places, low-confidence listings, Facebook-only churches, and
"websites" that are really directories, giving pages or a denomination's
national site. Many listings also carry an email address; it is saved in the
church list's `email` column and used as the contact when the church's own
website yields nothing (such rows get the status `listed`). The newest release
is used automatically; set `OVERTURE_RELEASE=2026-09-23.1` (any release name
on the bucket) to pin one.

**OpenStreetMap** only lists churches whose website a volunteer has added, so
it finds fewer, but a few that Overture lacks. `--source all` (the default)
runs both and merges the lists, dropping duplicate websites. If one of the
two services is down, the run carries on with the other.

### Optional: `.env` file

`python -m church_finder nightly sync` needs a WarmySender API key. Put it in
a `.env` file in the project folder so you never have to type it:

```bash
cp .env.example .env
# then edit .env and paste your key after the = sign
```

`.env` is listed in `.gitignore`, so the key stays on your machine and never
gets committed.

### Running on GitHub

Nothing in this repository runs on a schedule. The results found so far, the
WarmySender ledger and the outreach email copy live in a separate private
repository, `katiejallred/church-finder-data`. The `nightly` subcommands
(`plan`, `run`, `merge`, `sync`) work locally against a clone of it in a
`data` folder.

### Pushing to WarmySender

The **Sync to WarmySender** workflow is a manual button in the Actions tab.
It needs two repository secrets: `WARMYSENDER_API_KEY`, and `DATA_REPO_TOKEN`,
a fine-grained personal access token limited to the `church-finder-data`
repository with read and write access to Contents. Each run spends most of
its time waiting out WarmySender's rate limit (50 requests a minute), which
costs nothing because this repository is public. The same sync also runs from
your own computer:

```bash
# once: get the private data repo next to the code, and put your key in .env
git clone https://github.com/katiejallred/church-finder-data.git data
cp .env.example .env   # then paste WARMYSENDER_API_KEY=... into .env

# each time
git -C data pull
python -m church_finder nightly sync --data data --max 1000 --dry-run   # preview
python -m church_finder nightly sync --data data --max 1000
git -C data add -A && git -C data commit -m "WarmySender sync" && git -C data push
```

Committing the `data` folder afterwards keeps the ledger of pushed addresses
up to date, so the next sync (local or on GitHub) never pushes anyone twice.

## Output columns

| Column | Meaning |
| --- | --- |
| `best_email` | The best contact found |
| `contact_type` | `Communications`, `Admin / Office`, `Pastor` or `Other` |
| `role_title` | The title it was listed under, e.g. "Director of Communications" |
| `score` | Confidence of the role match (100+ is a comms director) |
| `found_on_page` | Page the email was found on, so you can check it; `listed in Overture Maps` for an address from the church's listing |
| `backup_email` / `backup_contact_type` | A second contact from a different tier (e.g. the church office) |
| `all_emails` | Up to 10 addresses found, best first |
| `status` | `ok`, `listed` (nothing usable on the website; the email is from the church's Overture listing), `no_email`, `fetch_error`, `robots_blocked` or `invalid_url` |

## How contacts are ranked

| Score | Type | Examples |
| --- | --- | --- |
| 100 | Communications | Communications Director, Director of Communications, Marketing Manager |
| 85–90 | Communications | Communications Coordinator, `communications@` / `media@` mailboxes |
| 80 | Communications | Creative Director, Media Director, Social Media Coordinator |
| 65 | Admin | Church Administrator, Executive Pastor, Operations Director |
| 50–55 | Admin | Office Manager, Administrative Assistant, `office@` / `info@` mailboxes |
| 45 | Pastor | Senior / Lead Pastor, Rector |
| 35–40 | Pastor | Pastor, Reverend, Priest, `pastor@` mailbox |
| 20 | Other | Youth / Worship / Children's Pastor |
| 10 | Other | Any other address |

Addresses on the church's own domain get a +5 bonus. Titles are matched
to the nearest staff card, so one person's title isn't given to the person
beside them. It also decodes Cloudflare-hidden emails and addresses written
like `name [at] church [dot] org`. Addresses with a made-up domain ending
(`pastor@st.luke`), template placeholders (`info@mydomain.com`) and the
support addresses of church-website vendors are thrown out.

## Limits

- Overture lists what Facebook and Microsoft know. A church's "website" there
  is often its Facebook Page or a directory listing; those are skipped, so some
  churches that do have a real website are missed when only the Facebook Page
  was listed. Categories come from the source listings too, so a few
  non-churches (a Christian school, a ministry office) slip in and the
  `denomination` column is only as right as the listing. You can always export
  a list from another source (a denomination directory, a data vendor) and use
  `scrape -i`.
- OpenStreetMap only lists churches whose website someone has added; coverage
  is good in cities and thinner in rural areas.
- Sites that only show staff through JavaScript, or only offer a contact form,
  come back as `no_email`.
- Facebook-only churches are skipped.

## Responsible use

The scraper honours `robots.txt`, waits between requests to each site, and
only reads public pages. If you email the contacts you find:

- Follow **CAN-SPAM**: use a truthful subject and sender, include your
  physical mailing address, and give an easy way to opt out. Honour
  opt-outs within 10 days.
- Keep a suppression list and never re-add people who opted out.
- Send small, relevant, personalised batches. Mass cold email to scraped
  lists hurts your sender reputation and your relationship with churches.
- Check any state laws that apply to you.

## Development

```bash
pip install pytest
python -m pytest
```

The **Tests** workflow runs the same suite on GitHub once per pull request and
on pushes to `main`, and only when code changes, so a run takes about a minute
of Actions time.
