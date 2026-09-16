# Isolate the Operator Console by Host within the Web runtime

- Status: accepted and implemented
- Scope: the protected Operator browser boundary

The public site and Operator Console share one Python Web runtime and PostgreSQL state so the
Operator projection can reuse the existing Source, Scheduler, EditorialWorkflow, and retrieval
index read seams without introducing a second service or policy implementation. They use distinct
exact HTTPS Hosts, entry points, APIs, static assets, and security policies; the application
revalidates the Host even behind Caddy, returns 404 when either surface crosses its Host boundary,
and authorizes Operator reads through server-side GitHub OAuth sessions keyed by stable numeric
user IDs. This keeps deployment small and editorial policy singular at the cost of making Host
routing and packaged Operator assets part of the protected Web boundary. Revisit the shared
runtime only if independently scaled Operator traffic, multiple operator roles, or service-level
isolation becomes a demonstrated requirement.
