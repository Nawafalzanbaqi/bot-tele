# 0002. Clean Architecture with enforced layering

- Status: Accepted
- Date: 2026-08-01

## Context

A self-hosted media hub outlives its dependencies. Over a few years FastAPI may
be replaced, the database may change, a Telegram or CLI front end may be added,
and download providers will certainly come and go. The business rules - what a
media item is, when a job may be retried - should survive all of that untouched.

The common alternative is the framework-shaped layout (`models.py`, `views.py`,
`services.py`). It is faster for the first month and then loses: business rules
end up inside route handlers and ORM models, so they can only be tested by
booting a web server and a database, and replacing either means rewriting the
rules.

Layering conventions that live only in documentation get violated under
deadline pressure, usually in a way nobody notices for months.

## Decision

Organise the system into four concentric layers - `domain`, `application`,
`infrastructure`, `presentation` - plus a dependency-free `shared` package for
configuration and logging. Source dependencies point inward only.

Enforce the rule with an executable test
(`tests/architecture/test_layer_dependencies.py`) that parses every module's
imports and fails CI with the offending file and import. The domain is
additionally forbidden from importing *any* third-party package.

Within the presentation layer, only the composition root (`app`, `lifespan`,
`dependencies`) may import `infrastructure`; routers and schemas must not know
which adapters exist.

## Consequences

- Domain and application logic are tested without a database, a network or a
  web server. The full suite runs in about two seconds.
- Swapping an adapter is a one-file change in the composition root.
- A new delivery mechanism is a new package that calls existing use cases.
- More indirection than a framework-shaped layout: a use case, a DTO and a port
  where some projects would have one function. That cost is real and accepted;
  it buys the properties above.
- Simple CRUD paths pay the same structural overhead as complex ones. We accept
  the uniformity, because "which parts deserve the structure" is a judgement
  that has to be re-made on every change and gets it wrong under pressure.
