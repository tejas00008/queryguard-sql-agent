-- Runs once, on first container start.
--
-- The agent NEVER connects as postgres. It connects as queryguard_ro, which
-- physically cannot write: every transaction it opens starts read-only, and
-- it holds no INSERT/UPDATE/DELETE/TRUNCATE grant to fall back on. If my
-- static validator has a bug and a write slips through, the database still
-- refuses it. Defence in depth is the whole point -- the validator is a
-- usability feature, this is the actual security boundary.

CREATE ROLE queryguard_ro WITH LOGIN PASSWORD 'readonly';

-- Every transaction this role opens is read-only, regardless of what it sends.
ALTER ROLE queryguard_ro SET default_transaction_read_only = on;

-- Server-side kill switch for runaway queries. The application also applies a
-- timeout, but an application timeout leaves the query running on the server.
ALTER ROLE queryguard_ro SET statement_timeout = '10s';

-- No temp tables, no scratch space to write into.
REVOKE TEMPORARY ON DATABASE queryguard FROM queryguard_ro;
REVOKE ALL ON SCHEMA public FROM queryguard_ro;
GRANT USAGE ON SCHEMA public TO queryguard_ro;

-- Tables don't exist yet (the loader creates them as postgres), so grant
-- SELECT on anything postgres creates in public from here on.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    GRANT SELECT ON TABLES TO queryguard_ro;
