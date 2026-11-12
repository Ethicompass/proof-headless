# The universal natural-query prior

A pool of phrasings real users of an insurance assistant send, in the register they send them.
The realism estimator measures every category the Exploiter generates against it, so a category
that drifts into stilted or overtly adversarial wording is penalised.

**This is a rehearsal seed, not an engagement's traffic.** gaussia ships no pool and says why: the
user's own traffic is what makes the estimate mean anything, and an invented pool measures our
imagination. This one exists so the exploitation path can be exercised end to end with the mock
target. A real engagement publishes its own -- `POST /priors` on compute -- and names it in the
run spec as `realism_prior`. Nothing about a client's realism budget should ever rest on this file.

## What this seed cannot do, measured

The realism estimator recommends `delta` from the pool's own spread, and twenty-four generic
phrasings are a tight pool: it recommends about **0.39**. Every category the search then writes
about a specific invented policy lands at a realism gap of **0.57 to 0.74**, so the search rejects
all of them before sending a single query and reports zero categories. Measured on 2026-09-02 with
`nvidia/nemotron-3.5-lightning` writing the queries and `qwen/qwen3-embedding-8b` embedding them.

That is not the estimator being wrong. It is the pool being ours. A client's real traffic is wider
and more specific, its spread recommends a larger budget, and queries about that client's products
sit inside it. The search proposes nothing until that pool exists, which is the honest outcome.
