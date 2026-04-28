# dbt Pain — Deep Dive into the Lived Experience

A research dossier of direct quotes, failure stories, and indirect signals about
what hurts when you actually use dbt in production. Sourced from Hacker News,
Substack (Pedram Navid, Benn Stancil, Max Halford), Reddit r/dataengineering,
GitHub issues on dbt-core / dbt-snowflake, dbt Discourse, vendor postmortems
(Datafold, SYNQ, Elementary, Monte Carlo, Select Star), and the 2024/2025
State of Analytics Engineering surveys.

The structure follows the four activities — **Writing models, Testing,
Validating, Maintaining** — then closes with cross-cutting patterns.

---

## 1. Writing Models

### Direct quotes

**On Jinja templating as a foreign body in SQL:**

> "Jinja syntax is harder to read and write than the worst SQL some engineers
> have ever seen."
> — recurring r/dataengineering complaint, summarized at
> [Orchestra: dbt Best Practices Reddit Insights](https://www.getorchestra.io/guides/data-build-tool-reddit---dbt-best-practices-community-insights)

> "Macros are a jinja-powered hot mess of untestable code, yet they remain the
> predominant way in which any logic outside of SQL is done in dbt."
> — Pedram Navid,
> [We need to talk about dbt](https://databased.pedramnavid.com/p/we-need-to-talk-about-dbt)

> "If you have worked with dbt for more than a week, you have probably
> encountered a frustrating debugging session where everything looks correct,
> but your model refuses to compile."
> — Alwyn DSouza,
> [Don't Nest Your Curlies](https://medium.com/towards-data-engineering/dont-nest-your-curlies-a-practical-guide-to-jinja-in-dbt-669eea3286fb)

> "Overusing Jinja, such as turning every repeated line into a macro, makes
> models harder to onboard new engineers to, harder to review in pull
> requests, and harder to debug."
> — same source, summarizing the macros-as-debt failure mode

**On the syntax-validation gap (no compile errors until you run dbt):**

> "The year is 2022, and I still need to run dbt before I can catch a syntax
> error."
> — Pedram Navid,
> [We need to talk about dbt](https://databased.pedramnavid.com/p/we-need-to-talk-about-dbt)

**On the `ref()` macro as ergonomic friction:**

> "I hate that you have to specify dependencies by hard-coding them with
> `ref(something)`. I can't just copy/paste my SQL query into my database
> client and execute it. I have to fiddle about and remove the curly braces."
> — Max Halford,
> [A rant against dbt ref](https://maxhalford.github.io/blog/dbt-ref-rant/)

> "When dbt determines the dependencies, builds the execution DAG, and runs
> the queries in the resulting order, it won't magically indicate you forgot
> to specify a `ref`."
> — Max Halford, same post — describing silent dependency bugs that surface
> only when parallel execution races against a stale upstream.

**On namespace collisions at scale:**

> "Given a dbt project of sufficient size, odds converge to 1.0 that two
> tables will want the same name, yet namespaces are not supported in refs."
> — Pedram Navid, "We need to talk about dbt"

**On dbt Cloud IDE as a development environment:**

> "dbt Cloud is a really bad experience... it's nothing more than a text
> editor with some syntax highlighting."
> — Pedram Navid, "We need to talk about dbt"

> "Loading it is exceptionally slow... I'll end up closing the window and
> switching to the terminal instead."
> — Pedram Navid, same post — also reports losing work when forgetting to
> switch branches before starting.

> "IDE sessions typically take 15-30 seconds to start, with some experiencing
> upwards of 2 minutes or longer ... [the IDE was] far too slow to start up
> and respond, with Git actions, saving, renaming and creating files all
> being terribly slow compared to VSCode."
> — summarized from
> [dbt Labs IDE performance updates blog](https://www.getdbt.com/blog/improvements-to-the-dbt-cloud-ide)
> and dbt Discourse "Your IDE session has timed out" thread.

**On forced bifurcation between SQL and YAML:**

> "How often have you updated a column in your model and not bothered to edit
> the schema file because it was too far away?"
> — Pedram Navid,
> [dbt Reimagined](https://databased.pedramnavid.com/p/dbt-reimagined)

### Failure stories

1. **The "spent two hours figuring out why my model wouldn't compile" pattern.**
   The Towards Data Engineering Jinja guide opens with the canonical scenario:
   "everything looks correct, but your model refuses to compile" — nested
   `{% %}` inside string interpolations, whitespace control characters
   (`{%- -%}`) silently swallowing trailing newlines that another macro
   depends on, and `{{ ref('foo') }}` failing because of an invisible BOM.
   The debug loop is: change one curly, `dbt compile`, wait, read error,
   repeat. ([source](https://medium.com/towards-data-engineering/dont-nest-your-curlies-a-practical-guide-to-jinja-in-dbt-669eea3286fb))

2. **"The team renamed a column and broke 14 dashboards over the weekend."**
   The DataChef "Missing Right Side of Your dbt DAG" article codifies this:
   "When you're editing a dbt model, a seemingly minor change (like renaming
   a column or updating a WHERE clause) can quietly break dozens of
   downstream charts. Without a safe place to test both sides together,
   teams either slow down their pace of development or risk introducing
   errors into content for end users." dbt's DAG sees only what is upstream
   of the model — it has no visibility into Looker, Mode, Tableau,
   reverse-ETL syncs, or ML jobs.
   ([source](https://blog.datachef.co/the-missing-right-side-of-your-dbt-dag))

3. **The macro time bomb at 47 callers.**
   The "Your dbt Macros Are Technical Debt Time Bombs" post describes:
   "real scenarios where macros written 18 months ago were called by 47
   models but the original author was no longer available, creating
   significant refactoring risks." Nobody is sure what the macro does;
   nobody is sure what will break if it is changed; the macro stays in
   place and accumulates a halo of workarounds.
   ([source](https://medium.com/@reliabledataengineering/your-dbt-macros-are-technical-debt-time-bombs-refactoring-without-breaking-8e68af2cc201))

### Indirect signals (what people complain about sideways)

- **`SELECT *` aversion via `dbt_utils.star`.** Teams reach for
  `{{ dbt_utils.star(from=ref('upstream'), except=['x']) }}` to "avoid
  enumerating columns" — the SQLMesh migration writeup notes that this
  apparent convenience comes with "the maintenance burden, the opacity of
  the transformations, and the challenges in debugging and optimization,"
  because the resolved column list is hidden until compile time.
  ([source](https://www.harness.io/blog/from-dbt-to-sqlmesh))
- **The "I just compile it locally and copy-paste into Snowflake to debug"
  workflow** is endemic — this is why Max Halford's `ref()` complaint
  resonates. The fact that people maintain a shadow workflow outside dbt
  to actually understand what their models do is itself a complaint.
- **"Build fast and messy" is the default at velocity-pressured shops.**
  "Teams often face a trade-off: build dbt models carefully while meeting
  all best practices, or build them fast and messy, and worry about tech
  debt later." The framing of best-practice as "the slow option" is a tell.
  ([source](https://stellans.io/dbt-project-structure-conventions/))
- **The proliferation of "dbt power user" VS Code extensions, dbt-coverage
  packages, dbt-checkpoint pre-commit hooks, dbt-osmosis, dbt-autofix.**
  Each one exists because the core developer experience leaves a gap a
  third-party feels obligated to close.

---

## 2. Testing

### Direct quotes

**On why teams skip tests / let them go red:**

> "Once teams proceed with merging despite a failing test with the reasoning
> 'we all know this one test is noisy, so we just been ignoring it,' they
> step (and set their team) on a slippery slope of allowing more and more
> tests to fail, eventually leading to tests not being taken seriously and
> significant data quality issues slipping through."
> — Elementary,
> [dbt tests: How to write fewer and better data tests](https://www.elementary-data.com/post/dbt-tests)

> "With more pipelines, models, and tests across the stack, the number of
> alerts grew. Important alerts were sometimes drowned out by noisy ones,
> and engineers frequently cited 'alert fatigue' as a pain point."
> — Elementary,
> [dbt observability 101](https://www.elementary-data.com/post/dbt-observability-101-how-to-monitor-dbt-run-and-test-results)

**On generic tests as low-value noise:**

> "Some tests are inherently noise, poorly-defined, or redundant. It's ok
> to remove a test if you believe it's not adding incremental value."
> — Datafold,
> [7 dbt testing best practices](https://www.datafold.com/blog/7-dbt-testing-best-practices/)

> "Just because you can write a test doesn't mean you should."
> — same source, on the cargo-culting of `not_null`/`unique`/`accepted_values`
> on every column of every model.

**On unit-test adoption being near-zero despite dbt 1.8 shipping it:**

> "Unit testing arrived in dbt 1.8, but nobody does it. The YAML accounting
> killed adoption before it started, with writing fixtures by hand, sampling
> data, and formatting dictionaries creating too much friction. More
> specifically, developers must figure out which models their test depends
> on, query the warehouse to get realistic sample data, format everything as
> YAML dictionaries with the right structure, then do it again for every
> edge case they want to cover and maintain all of it as models evolve."
> — David Rubio,
> [dbt test coverage: the missing SLI in your data platform](https://medium.com/data-science-collective/dbt-test-coverage-the-missing-sli-in-your-data-platform-55f019fdcd93)

> "In SQL there is still no equivalent to test coverage, and even with dbt
> tests, it's hard to answer a simple question: How much of SQL logic is
> actually tested?"
> — same source — on why teams have no honest measure of whether their
> tests cover anything load-bearing.

**On the false comfort of green-test runs:**

> "Copy-pasting YAML without understanding what you're testing is how you
> end up with a green CI pipeline and broken dashboards."
> — Adrienne Vermorel,
> [Unit Testing dbt Models: Real-World Examples and Patterns](https://adriennevermorel.com/articles/unit-testing-dbt-real-world-examples-patterns/)

> "Incremental models are the most common source of subtle bugs in dbt
> projects because they have two completely different code paths — one that
> runs on a full refresh, another that runs during normal incremental loads
> — and teams often test only one of them. A typical failure mode looks
> like: you develop your incremental model and it works perfectly in dev,
> deploy to production and run a full refresh with everything looking great,
> but three months later someone notices duplicates appearing in downstream
> reports because the incremental logic had a bug that only manifested
> after months of daily runs."
> — same source.

### Failure stories

1. **The Monday-morning ARR doubling.**
   Select* From's "The Cost of Silent dbt Failures" opens with:
   "A null value in a join key caused a 'massive fan-out,' doubling the
   'Total ARR' figure overnight. The discovery process took until 9:15 AM
   when the Head of Sales noticed the anomaly. Resolution required three
   hours of manual debugging through dbt models and SQL code. A simple
   5-line YAML test could have caught this on Friday."
   ([source](https://selectstarfrom.substack.com/p/the-cost-of-silent-dbt-failures-a))

2. **Two and a half weeks of lost data because nobody added a freshness test.**
   From a TowardsDataScience post on source freshness: "I've dealt with
   issues in the past where ingestion tools said data was being ingested
   into the warehouse, only to find out that it wasn't properly working for
   over two weeks. This resulted in two and a half weeks of lost data! If
   I had added freshness tests at the source earlier, this would have never
   happened." dbt's source freshness was always available — the team simply
   didn't wire it in, because it isn't on by default.
   ([source](https://towardsdatascience.com/how-fresh-are-your-data-sources-e8db53cf4653/))

3. **Half-populated incremental table going green for months.**
   "When a source system adds a new column, the incremental model keeps
   running every hour and everything shows green with dashboards looking
   fine, but the table becomes split — half complete with the new data and
   half incomplete without it. The model doesn't fail and keeps adding data
   while ignoring the new column, so data checks might never catch this
   since nothing technically broke — you just stopped capturing important
   business information."
   ([source](https://medium.com/@pathak12darshan/dbt-incremental-models-can-quietly-break-your-data-heres-how-to-fix-it-836c90ce1963))

4. **Unique-key duplicates that survive `unique` tests.**
   The dbt-core issue tracker has a long-standing class of bugs (issues
   [#7873](https://github.com/dbt-labs/dbt-core/issues/7873),
   [#7597](https://github.com/dbt-labs/dbt-core/issues/7597),
   [#5691](https://github.com/dbt-labs/dbt-core/issues/5691)) where
   `incremental` models with NULL values inside the unique key silently
   append duplicates. The downstream `unique` test then either passes
   (because the test runs after the dedup the developer assumed was
   happening) or fails opaquely days later when the NULL pattern shows up
   in production data. The dbt Discourse thread "incremental model + unique
   constraint still allows duplicates" runs into this from the user side.
   ([discourse](https://discourse.getdbt.com/t/incremental-model-unique-constraint-still-allows-duplicates/17298))

### Indirect signals

- **`severity: warn`** is a feature whose existence signals "tests fail
  too often to block CI." A team that sets `severity: warn` on a `unique`
  test has effectively decided the test is wrong but hasn't yet decided
  what right looks like.
- **Elementary, Monte Carlo, SYNQ, re_data, Datafold all sell "data
  observability" — i.e. the layer that catches what dbt tests miss.**
  The market exists because the in-tree test surface is acknowledged
  insufficient. Mikkel Dengsøe (SYNQ) frames this directly: "A dbt model
  failing may be an early warning sign of a trivial issue or indicate
  that your entire pipeline is down, and context is needed to make this
  call."
  ([source](https://medium.com/@mikldd/learnings-from-running-hundreds-of-data-incidents-at-synq-22478f017472))
- **The 2024 State of Analytics Engineering** found "poor data quality
  emerged as a predominant issue for 57% of professionals, an increase
  from 41% in 2022." Two-plus years into widespread dbt adoption,
  data-quality complaints are getting *worse*, not better.
  ([source](https://www.bigdatawire.com/2024/04/05/data-quality-getting-worse-report-says/))
- **Test running cost as a hidden tax.** "dbt tests (especially uniqueness,
  not_null, and relationships) generate SQL queries that scan full tables,
  and running tests on billion-row fact tables daily can burn significant
  credits." Teams disable expensive tests for cost, leaving big tables
  least-covered.
  ([source](https://medium.com/@manik.ruet08/the-hidden-costs-of-dbt-snowflake-and-how-to-fix-them-24fac0639fef))

---

## 3. Validating (CI / Breaking-Change Detection)

### Direct quotes

**On Slim CI's structural blind spot:**

> "With Slim Diff configuration, downstream models will be prevented from
> running unless they have been designated as exceptions with the
> slim_diff: diff_when_downstream dbt meta tag. This suggests that care
> must be taken to identify which downstream models truly need to be
> tested, as the default behavior may not catch all potential breaking
> changes that could escape to production."
> — Datafold,
> [Slim CI: Cost-Effective Solution](https://www.datafold.com/blog/slim-ci-the-cost-effective-solution-for-successful-deployments-in-dbt-cloud/)

**On dbt CI testing syntax not behavior:**

> "A column rename or semantic logic change can still break a dashboard
> even if the exposure compiles."
> — DataChef,
> [The Missing Right Side of Your dbt DAG](https://blog.datachef.co/the-missing-right-side-of-your-dbt-dag)

**On Datafold pricing as the "real CI" tax:**

> "Datafold's Cloud tier starts at $799/month when billed annually. For
> larger deployments, small to mid-sized teams (5–15 data sources, <10TB
> data) typically pay annual contract values ranging from $30,000–$75,000
> for cloud-hosted deployments."
> — [Vendr Datafold listing](https://www.vendr.com/marketplace/datafold)

> "Datafold has a commercial subscription requirement, which posed
> challenges for adopting and using the tool effectively."
> — G2 reviewer summary,
> [Datafold Reviews](https://www.g2.com/products/datafold/reviews)

**On dbt Cloud Advanced CI as the bundled-but-priced version:**

> "Over the last 6 months, dbt Cloud pricing has changed drastically, with
> a 100–700% increase from Dec-2022, followed by new 'Shiny New Pricing'
> that will cut even deeper holes into already stretched analytics
> budgets."
> — Paradime,
> [What's the new dbt Cloud price increase about? — Part 2](https://www.paradime.io/blog/whats-the-new-dbt-cloud-tm-price-increase-about-part-2)

**On the structural CI gap dbt cannot close on its own:**

> "If you maintain a dbt project long enough, you end up with a problem:
> you can see what's upstream of a model, but it is surprisingly hard to
> answer what is downstream in the real world. Which dashboards, reports,
> ML jobs, or extracts depend on this thing, and who owns them?"
> — DataChef, "Missing Right Side"

### Failure stories

1. **The exposure compiles, the dashboard breaks anyway.**
   The Omni community tells the canonical version: a dbt model edit passes
   `dbt build`, all unit tests pass, all data tests pass, the PR merges,
   the next morning the Looker dashboard renders zeros because a column
   semantic changed (e.g., `revenue` switched from cents to dollars; the
   column name didn't change). dbt's contract enforcement caught nothing
   because the schema didn't change.
   ([source](https://community.omni.co/t/testing-dbt-changes-in-omni-before-pushing-to-production-omnis-dynamic-dbt-environments/401))

2. **The "we don't actually test data in CI, we test SQL syntax" admission.**
   Slim CI's value prop — "only build modified models and downstream of
   modified models" — is *itself* a workaround for the fact that running
   the full DAG in CI is too slow / expensive. The trade-off is that
   you're now CI-testing a fraction of the project against a *production
   manifest* of unchanged parents — which means the PR runs against
   production data the developer hasn't seen, and the feedback loop is
   "did the SQL parse and execute?" rather than "did the data come out
   right?". Datafold's Slim Diff and dbt's Advanced CI exist as paid
   layers on top to add row-level diffing because Slim CI alone gives
   you syntactic comfort and semantic blindness.

3. **The first-run incremental policy bug.**
   "If there is an error in your incremental policy, the first run will
   still be successful and dbt will not throw an error until the second
   run. This can be especially troublesome for models run overnight once
   per day — the engineer will manually test their model during business
   hours thinking it works, then the stakeholder gets a failure message
   after hours saying the data is now stale."
   ([source](https://medium.com/@JosephOjo/solving-a-silent-bug-in-dbt-fe634c3c44f7))

4. **Breaking-DDL detection is missing.**
   dbt-fusion issue [#1532](https://github.com/dbt-labs/dbt-fusion/issues/1532)
   ("Feature Proposal: Safe Incremental Rebuild with Automatic Breaking
   DDL Detection"): "When a developer makes structural changes to an
   incremental model — reordering columns, inserting a column in the
   middle, renaming a column, or changing incompatible data types — dbt
   has no automatic recovery path and the run either errors at runtime
   or produces silent data corruption." This is open in 2026 — i.e., the
   foundational tool still doesn't detect a class of breaking change that
   ships to prod weekly.

### Indirect signals

- **The fact that "Slim CI vs. Advanced CI vs. Datafold vs.
  re_data vs. Synq vs. SaaS-of-the-week" is even a conversation** is a
  tell that no one tool gives operators what they need from a CI run.
  Teams stack three or four because each catches a different blind spot
  (syntax, schema, row-diff, semantic, freshness).
- **dbt Mesh model contracts are themselves a symptom.** The product
  literature says contracts are about "graceful evolution"; the
  practical use is "stop my downstream consumers from yelling at me when
  I change a column." The fact that contracts are an *opt-in advanced
  feature* rather than the default means most projects ship without
  them, and the breaking-change detection story degrades to
  "tribal knowledge + Slack apology."
- **The "no announcement, just a blog post" rollout of consumption-based
  pricing** (per Pedram Navid) signals that even the vendor treats CI
  cost as something to be quietly billed for, rather than a first-class
  capability.

---

## 4. Maintaining

### Direct quotes

**On stale model graveyards:**

> "dbt models persist in your production database even after they're
> deleted from your project, adding clutter to the warehouse and
> potentially slowing down operations like database or schema cloning."
> — summarized in the
> [dbt Discourse "Clean your warehouse of old and deprecated models"](https://discourse.getdbt.com/t/clean-your-warehouse-of-old-and-deprecated-models/1547)
> thread.

**On the macro time-bomb again, this time from a maintenance angle:**

> "Macro complexity grows exponentially with team size and project age,
> where what seems clever at 50 models becomes unmaintainable at 500
> models."
> — [Reliable Data Engineering](https://medium.com/@reliabledataengineering/your-dbt-macros-are-technical-debt-time-bombs-refactoring-without-breaking-8e68af2cc201)

**On Snowflake cost explosions caused by dbt:**

> "Snowflake Ate My Budget: The 'Quick' Query That Turned Into an $18K
> Surprise … a single ad-hoc query that ran for almost 5 hours, scanned
> hundreds of terabytes, and burned $18,300 in credits since midnight —
> a week's worth of spend in just a few hours."
> — Abhishek Kumar Gupta,
> [Snowflake Ate My Budget](https://medium.com/tech-with-abhishek/snowflake-ate-my-budget-the-quick-query-that-turned-into-an-18k-surprise-dea4894e2785)

> "Storing raw, intermediate, and historical tables for years can silently
> bloat costs, and dbt's habit of generating 'backup' or 'snapshot' tables
> causes storage charges to creep up. … Nested models, redundant CTEs,
> and wide joins can cause Snowflake to re-scan the same data multiple
> times, with dbt DAGs sometimes looking clean but having wasteful
> queries under the hood."
> — Manik Hossain,
> [The Hidden Costs of dbt + Snowflake](https://medium.com/@manik.ruet08/the-hidden-costs-of-dbt-snowflake-and-how-to-fix-them-24fac0639fef)

> "We didn't realize dev environment refreshes counted as runs."
> — anonymous data team lead, recounted in same article — re: dbt Cloud
> consumption-based billing surprise.

**On onboarding pain:**

> "Onboarding business people/analysts to dbt requires teaching them SQL,
> dbt, command line, git, pull requests, CI, and unless you are on dbt
> Cloud it also requires a basic knowledge of python environments."
> — r/dataengineering, summarized at
> [Orchestra](https://www.getorchestra.io/guides/data-build-tool-reddit---dbt-best-practices-community-insights)

> "When analysts first encounter dbt, they often feel confused and
> uncertain, and dbt's technical terminology can induce impostor
> syndrome, even for those who know SQL and data modeling concepts."
> — dbt Labs' own
> ["Learning dbt as an analyst"](https://www.getdbt.com/blog/learning-dbt-as-an-analyst)
> blog post — a tell that even the vendor acknowledges the cliff.

> "Setting up a DBT project is challenging, and managing versioning,
> syntax, spacing, and linting across developers is a nightmare."
> — recurring r/dataengineering complaint
> ([source](https://www.getorchestra.io/guides/data-build-tool-reddit---dbt-best-practices-community-insights))

**On scale beyond ~500 models:**

> "<500 models … reflects the point at which dbt Labs' own internal
> analytics project went from feeling 'manageable' to 'there's too much
> going on.'"
> — [dbt Mesh: Who is dbt Mesh For](https://docs.getdbt.com/best-practices/how-we-mesh/mesh-2-who-is-dbt-mesh-for)
> — i.e., the vendor's own bar for "this got hard" is 500 models, and
> "over the past year, the number of 'large' projects (>500 models) has
> tripled."

**On the Cloud price increases as a maintenance cost shock:**

> "dbt increased the price of dbt Cloud by 100% on December 15, 2022 on
> non-enterprise pricing tiers, though the product had not materially
> changed or was not necessarily providing more value with the price
> increase. … With the Team tier and 8 seats, customers would now be
> paying 225% more than before."
> — Paradime,
> [What's the new dbt Cloud price increase about?](https://www.paradime.io/blog/whats-the-new-dbt-cloud-tm-price-increase-about-part-2)

**On debugging at scale:**

> "Debugging in dbt can be less straightforward than in other environments,
> with errors propagating through the transformation pipeline and making
> pinpointed debugging tedious."
> — recurring complaint, summarized at
> [Orchestra: Downsides of dbt](https://www.getorchestra.io/guides/downsides-of-dbt-challenges-solutions)

> "Debugging tasks are difficult because error messages lack clarity,
> resulting in analysts spending a lot of time on logs."
> — same source

### Failure stories

1. **The 6-8 hour daily refresh.**
   "A dbt Cloud user was spending 6-8 hours per day on pipeline refreshes
   before migrating to dbt." The framing is a success story (they got out
   of stored procs) but the reality of the pre-state — most of a workday
   spent watching pipelines — is the maintenance cost of the legacy
   approach dbt was meant to displace, and many of those teams now spend
   the equivalent on dbt full refreshes.
   ([source](https://www.getdbt.com/blog/stored-procedures-dbt-migration-playbook))

2. **The "we lost an analyst" pattern.**
   The recurring r/dataengineering complaint is that analytics-engineering
   onboarding now requires SQL + Jinja + YAML + git + PRs + CI + virtual
   envs. Every additional layer is a place where a junior analyst hits a
   "why doesn't this work" wall, has nobody to ask, and decides they'd
   rather be in the BI tool. The talent attrition isn't from dbt
   *failing*; it's from dbt being a five-tool job advertised as one tool.

3. **The full-refresh outage.**
   dbt-core issue [#12467](https://github.com/dbt-labs/dbt-core/issues/12467)
   ("[Bug] full-refresh broken for too large data") — running
   `dbt run --full-refresh` on roughly 5 million records in 35,000
   partitions resulted in `HIVE_PATH_ALREADY_EXISTS` after a long time,
   because temporary table directories already existed. The dbt
   Discourse "How to prevent (accidental) full refreshes" thread
   (issue #1008) exists because operators have learned the hard way
   that `--full-refresh` on the wrong night brings the warehouse to its
   knees and leaves a half-built table behind.

4. **The 4-minute `dbt deps`.**
   Issue [#11479](https://github.com/dbt-labs/dbt-core/issues/11479):
   "dbt deps runs very slow, each request waits 4 min before receiving
   response." Multiplied across CI runs, dev rebuilds, and onboarding
   sessions, this is a per-team-per-day tax in pure waiting.

5. **The 26% partial-parse regression.**
   Issue [#10127](https://github.com/dbt-labs/dbt-core/issues/10127):
   "[Performance] 1.8 slower partial parsing than 1.7." Going from 1.7.3
   to 1.8.0 raised partial-parse from 6.00s to 7.58s — a small absolute
   number that, multiplied by every dev iteration of every analyst at
   scale, becomes hours of waiting per week. Even the Fusion-engine
   marketing leans on this: "After years of community complaints about
   dbt Core's performance bottlenecks in larger projects, dbt Labs has
   delivered a solution."
   ([source](https://thedataprism.com/dbt-fusion-vs-dbt-core-a-complete-comparison-2025/))

### Indirect signals

- **The existence of `dbt-autofix`** — a CLI dbt Labs ships explicitly to
  walk projects and fix deprecations automatically — admits that the
  upgrade tax is high enough to need its own automation.
- **The `dbt-coverage` package** exists because operators want a
  measurable answer to "how much of our SQL is tested," and core dbt
  doesn't supply one.
- **The dbt Mesh product is itself an indirect signal**: the "graceful
  way to scale beyond a single repo" is necessary because a single repo
  beyond ~500 models stops working. The 2024 data shows "the number of
  'large' projects (>500 models) has tripled" — i.e., the number of
  projects in the failure zone is growing fast and dbt's prescription
  is "split into many projects with cross-project contracts," which
  trades one complexity for another.
- **The Fusion engine ships under Elastic License 2.0, not Apache 2.0.**
  Adapter ecosystem fragmentation ("Fusion adapters must be written in
  Rust rather than Python, making existing Core adapters incompatible
  without complete rewrites … For community contributors who may lack
  Rust expertise, this represents a significant barrier to
  participation") signals the maintenance burden is moving from users
  to adapter maintainers, who may not be there to take it.
  ([source](https://medium.com/@kayrnt/dbt-fusion-the-double-edged-sword-e49482ed793e))

---

## 5. Patterns Across All Four

### What experienced dbt users complain about that newcomers haven't hit yet

- **Silent dependency bugs from forgotten `ref()`s.** A newcomer reads
  every line of every model. A team with 800 models cannot — they trust
  the DAG. The DAG is only as complete as the `ref()` discipline at
  every keystroke, and there is no compile-time enforcement.
- **The macro time-bomb.** At 50 models, macros feel clever. At 500,
  the original author left, every model touches one of three macros, and
  nobody can refactor without breaking forty things.
- **Incremental-model edge cases.** The full-refresh path and the
  incremental path are two implementations of the same logic. They drift.
  The first time you notice is when the sales team asks why ARR halved
  between Tuesday and Wednesday.
- **The dev/prod cost asymmetry.** A junior runs `dbt build` in dev and
  burns nothing they can see. The bill arrives at the end of the month.
  By the time the team installs `dbt-snowflake-monitoring` and query
  tags, the spend chart already has a step function in it.
- **The Mesh tax at scale.** Teams that "got it working" with a
  monorepo + dbt Cloud + Slim CI hit a wall around 500-800 models and
  discover that the next mile requires Mesh, contracts, model versions,
  cross-project orchestration, and a tax on every change.

### What pain has been "solved" by tooling but still shows up because adoption is hard

- **Unit tests** (dbt 1.8) — solved on paper, "nobody does it" in
  practice because the YAML fixture authoring is too painful and there
  is no equivalent to test coverage to nag you about gaps.
- **Source freshness** — has been in dbt forever, requires opting in per
  source, and remains the "we lost two and a half weeks of data because
  we never wired it up" story.
- **Model contracts** — solved the schema-drift case for opted-in
  models, but require explicit declaration that most teams skip during
  initial development "to ship faster."
- **Column-level lineage** — exists in dbt Cloud Explorer and several
  third-party catalogs (Select Star, Atlan, Datahub, Castor), but
  requires the catalog to be wired in and refreshed; the "what
  dashboards depend on this column" question still routinely takes a
  Slack thread to answer.
- **Slim CI** — solved the "don't rebuild the world for every PR"
  problem, but in doing so introduced a *new* problem (you're CI-testing
  against a manifest you didn't author, on data the developer can't
  see), which sells the row-level-diff layer (Datafold, Advanced CI).

### What pain is structural to dbt's design philosophy

- **SQL-as-text + Jinja-on-top.** Because dbt treats SQL as a string the
  Jinja engine renders, it cannot in general parse the result for
  references, type-check across columns, or detect breaking renames at
  compile time. Fusion (Rust + a real SQL parser) is dbt Labs'
  acknowledgment of the cost — "Unlike dbt Core, which treats SQL as
  templated text, Fusion parses and understands SQL syntax and
  semantics across data platforms" — but the install base of
  Core-and-Jinja still owns the long tail.
- **`ref()` as the dependency anchor.** Max Halford's rant cuts to it:
  the explicit `ref()` is the only thing connecting your DAG to
  reality, and forgetting it produces silent breakage. SQLMesh (and
  Fusion) auto-detect dependencies from parsed SQL because the manual
  contract is the wrong default. dbt cannot change this without
  breaking every project ever written.
- **YAML configuration alongside SQL files.** The bifurcation between
  `model.sql` and `_model.yml` is the source of every "the docs are
  out of date" complaint, every "I forgot to add the column to the
  schema" failure, every "the test was on the old name" outage.
  Pedram Navid: "How often have you updated a column in your model
  and not bothered to edit the schema file because it was too far
  away?"
- **Tests run *after* the model materializes.** The test executes
  against the result table, not against the SQL. So the test can only
  catch data shapes, never code intent. That is why unit tests
  (introduced in 1.8) had to be a whole new construct — and why nobody
  uses them.
- **No first-class downstream awareness.** dbt is a pre-warehouse tool;
  it does not know about Looker, Tableau, Mode, Hex, reverse-ETL syncs,
  ML feature stores, or anything that reads its output. Exposures
  exist as a manual workaround. "What breaks if I rename this column?"
  is structurally unanswerable inside dbt; that is the entire reason
  observability vendors exist as a category.
- **Cost surfaces at the warehouse, not at dbt.** dbt has no native
  notion of "this model is expensive" until you wire in
  `dbt-snowflake-monitoring`, `dbt-bigquery-monitoring`, or a query-tag
  + per-warehouse dashboard. By design, dbt produces SQL and then
  walks away. By consequence, cost surprise is the single most common
  "we didn't realize" complaint in the entire ecosystem.
- **The dbt Cloud commercial trajectory.** Every structural pain above
  is an opportunity to sell a paid layer (Advanced CI, Explorer,
  Mesh + governance, Cost Insights, Fusion engine licensing). The
  100-700% price increase between Dec 2022 and 2024 reads, in this
  light, as the monetization of pains the open-source layer was
  always going to leave on the table — and the community reaction
  (Pedram Navid's posts; Tristan Handy's response post; SQLMesh's
  rise; Fusion's Elastic License 2.0) reflects an ecosystem that is
  no longer convinced the vendor and the user incentives are aligned.

---

## Source index

- Pedram Navid, [We need to talk about dbt](https://databased.pedramnavid.com/p/we-need-to-talk-about-dbt)
- Pedram Navid, [dbt Reimagined](https://databased.pedramnavid.com/p/dbt-reimagined)
- Pedram Navid, [What the hell is going on with data?](https://databased.pedramnavid.com/p/what-the-hell-is-going-on-with-data)
- Tristan Handy, [The response you deserve!](https://roundup.getdbt.com/p/the-response-you-deserve)
- Benn Stancil, [How dbt fails](https://benn.substack.com/p/how-dbt-fails)
- Max Halford, [A rant against dbt ref](https://maxhalford.github.io/blog/dbt-ref-rant/)
- Christophe Oudar, [dbt Fusion: The Double-Edged Sword](https://medium.com/@kayrnt/dbt-fusion-the-double-edged-sword-e49482ed793e)
- Hacker News, [I can't argue that dbt isn't great...](https://news.ycombinator.com/item?id=29425343)
- Hacker News, [Ask HN: What Is the Point of Dbt?](https://news.ycombinator.com/item?id=25887768)
- David Rubio, [dbt test coverage: the missing SLI in your data platform](https://medium.com/data-science-collective/dbt-test-coverage-the-missing-sli-in-your-data-platform-55f019fdcd93)
- Adrienne Vermorel, [Unit Testing dbt Models: Real-World Examples and Patterns](https://adriennevermorel.com/articles/unit-testing-dbt-real-world-examples-patterns/)
- Elementary Data, [dbt observability 101](https://www.elementary-data.com/post/dbt-observability-101-how-to-monitor-dbt-run-and-test-results)
- Elementary Data, [dbt tests: How to write fewer and better data tests](https://www.elementary-data.com/post/dbt-tests)
- Elementary Data, [My dbt test failed - now what?](https://www.elementary-data.com/post/my-dbt-test-failed-now-what)
- Datafold, [7 dbt testing best practices](https://www.datafold.com/blog/7-dbt-testing-best-practices/)
- Datafold, [Slim CI: The Cost-Effective Solution for Successful Deployments](https://www.datafold.com/blog/slim-ci-the-cost-effective-solution-for-successful-deployments-in-dbt-cloud/)
- Select* From, [The Cost of Silent dbt Failures](https://selectstarfrom.substack.com/p/the-cost-of-silent-dbt-failures-a)
- DataChef, [The Missing Right Side of Your dbt DAG](https://blog.datachef.co/the-missing-right-side-of-your-dbt-dag)
- Mikkel Dengsøe, [Learnings from running hundreds of data incidents at SYNQ](https://medium.com/@mikldd/learnings-from-running-hundreds-of-data-incidents-at-synq-22478f017472)
- Manik Hossain, [The Hidden Costs of dbt + Snowflake](https://medium.com/@manik.ruet08/the-hidden-costs-of-dbt-snowflake-and-how-to-fix-them-24fac0639fef)
- Abhishek Kumar Gupta, [Snowflake Ate My Budget](https://medium.com/tech-with-abhishek/snowflake-ate-my-budget-the-quick-query-that-turned-into-an-18k-surprise-dea4894e2785)
- Paradime, [dbt Cloud price increase analysis](https://www.paradime.io/blog/whats-the-new-dbt-cloud-tm-price-increase-about-part-2)
- Reliable Data Engineering, [Your dbt Macros Are Technical Debt Time Bombs](https://medium.com/@reliabledataengineering/your-dbt-macros-are-technical-debt-time-bombs-refactoring-without-breaking-8e68af2cc201)
- Alwyn DSouza, [Don't Nest Your Curlies: Jinja in dbt](https://medium.com/towards-data-engineering/dont-nest-your-curlies-a-practical-guide-to-jinja-in-dbt-669eea3286fb)
- Darshan Pathak, [dbt Incremental Models Can Quietly Break Your Data](https://medium.com/@pathak12darshan/dbt-incremental-models-can-quietly-break-your-data-heres-how-to-fix-it-836c90ce1963)
- Ojo Joseph, [Solving a Silent Bug in dbt](https://medium.com/@JosephOjo/solving-a-silent-bug-in-dbt-fe634c3c44f7)
- Harness, [Transitioning from dbt to SQLMesh](https://www.harness.io/blog/from-dbt-to-sqlmesh)
- The Data Prism, [dbt Fusion vs dbt Core: A Complete Comparison (2025)](https://thedataprism.com/dbt-fusion-vs-dbt-core-a-complete-comparison-2025/)
- Orchestra, [dbt Best Practices: Insights from the Reddit Community](https://www.getorchestra.io/guides/data-build-tool-reddit---dbt-best-practices-community-insights)
- Orchestra, [Downsides of dbt: Challenges & Solutions](https://www.getorchestra.io/guides/downsides-of-dbt-challenges-solutions)
- dbt-core issue [#12467 — full-refresh broken for too large data](https://github.com/dbt-labs/dbt-core/issues/12467)
- dbt-core issue [#11479 — dbt deps runs very slow](https://github.com/dbt-labs/dbt-core/issues/11479)
- dbt-core issue [#10127 — 1.8 slower partial parsing than 1.7](https://github.com/dbt-labs/dbt-core/issues/10127)
- dbt-core issue [#7873 — incremental model duplicates with NULL unique keys](https://github.com/dbt-labs/dbt-core/issues/7873)
- dbt-core issue [#7597 — duplicates if any unique_key fields are null](https://github.com/dbt-labs/dbt-core/issues/7597)
- dbt-fusion issue [#1532 — Safe Incremental Rebuild with DDL Detection](https://github.com/dbt-labs/dbt-fusion/issues/1532)
- dbt Discourse, [incremental model + unique constraint still allows duplicates](https://discourse.getdbt.com/t/incremental-model-unique-constraint-still-allows-duplicates/17298)
- dbt Discourse, [Clean your warehouse of old and deprecated models](https://discourse.getdbt.com/t/clean-your-warehouse-of-old-and-deprecated-models/1547)
- dbt Discourse, [How to prevent (accidental) full refreshes](https://discourse.getdbt.com/t/how-to-prevent-accidental-full-refreshes-of-a-model/1008)
- dbt Labs, [Learning dbt as an analyst](https://www.getdbt.com/blog/learning-dbt-as-an-analyst)
- dbt Labs, [The 2024 State of Analytics Engineering](https://www.getdbt.com/blog/the-2024-state-of-analytics-engineering-report)
- dbt Labs, [Updates to improving the dbt Cloud IDE performance](https://www.getdbt.com/blog/improvements-to-the-dbt-cloud-ide)
- BigDataWire, [Data Quality Getting Worse, Report Says](https://www.bigdatawire.com/2024/04/05/data-quality-getting-worse-report-says/)
- TowardsDataScience, [How Fresh Are Your Data Sources?](https://towardsdatascience.com/how-fresh-are-your-data-sources-e8db53cf4653/)
- Stellans, [dbt Project Structure Conventions](https://stellans.io/dbt-project-structure-conventions/)
- Vendr, [Datafold pricing listing](https://www.vendr.com/marketplace/datafold)
- G2, [Datafold reviews](https://www.g2.com/products/datafold/reviews)
- Omni Community, [Testing dbt changes in Omni before pushing to production](https://community.omni.co/t/testing-dbt-changes-in-omni-before-pushing-to-production-omnis-dynamic-dbt-environments/401)
- dbt Labs, [Stored procedures to dbt: a modern migration playbook](https://www.getdbt.com/blog/stored-procedures-dbt-migration-playbook)
- dbt-core docs Discussion, [Multi-project collaboration #6725](https://github.com/dbt-labs/dbt-core/discussions/6725)
- dbt Mesh docs, [Who is dbt Mesh for?](https://docs.getdbt.com/best-practices/how-we-mesh/mesh-2-who-is-dbt-mesh-for)
