-- Mothli is one hosted product (ADR 0043, step 8): readers never pay for the AI with their own AI
-- account, the platform pays. Steps 6 and 7 removed the routes, the settings screen, the retired
-- job and the worker's key lookup; what is left of the feature goes here: the encrypted keys
-- (0003, 0009, row security in 0004), the model choices made for them (0003, 0010, row security
-- in 0004) and the jobs column that named a key (0003), with its link to the keys and the check
-- 0028 added so that this drop takes it along.
--
-- If a row exists (nothing is deployed, so none should), it is erased with its table: a stored
-- key is a secret a reader gave Mothli, kept only for a feature that no longer exists, so erasing
-- it is the right outcome; a model choice names such a key and goes with it. No job can name a
-- key (0028's check), so dropping the column loses nothing. Every statement names what it drops,
-- and nothing cascades: a dependency this file does not name stops the migration whole.

-- The model choices first: they point at the keys.
DROP POLICY provider_model_settings_owner_isolation ON provider_model_settings;
DROP INDEX ix_provider_settings_owner_credential;
DROP TABLE provider_model_settings;

-- The jobs column: its link to the keys, the check that it is always empty, then the column.
ALTER TABLE jobs DROP CONSTRAINT jobs_owner_id_credential_id_fkey;
ALTER TABLE jobs DROP CONSTRAINT jobs_names_no_credential_ck;
ALTER TABLE jobs DROP COLUMN credential_id;

-- The keys. Their checks, primary key and owner link go with the table; grants on it go too, and
-- the role script stops granting it (app/hosted/tenant/grants.py).
DROP POLICY provider_credentials_owner_isolation ON provider_credentials;
DROP INDEX ix_provider_credentials_owner_live;
DROP TABLE provider_credentials;
