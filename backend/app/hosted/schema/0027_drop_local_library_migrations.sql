-- Mothli is one hosted product (ADR 0043, D3): nothing moves a library in from the local app any
-- more, so the record of those moves (migration 0013) goes. Nothing references the table, no role
-- is granted anything on it by name (the purge role's grant is found from the catalogue), and
-- dropping it takes its row security policy, checks, unique indexes and owner link with it.
DROP POLICY local_library_migrations_owner_isolation ON local_library_migrations;
DROP TABLE local_library_migrations;
