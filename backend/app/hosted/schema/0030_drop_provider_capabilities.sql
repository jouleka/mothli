-- The service table provider_capabilities (0001) was made for provider metadata that was never
-- built: no code reads or writes it, no role is granted it, and it holds no reader's content and
-- no secret (ADR 0043, left for later then). Mothli is one product whose AI the platform pays for,
-- so it is dropped. Its primary key and check go with it. Nothing cascades: a dependency this file
-- does not name stops the migration whole.
DROP TABLE provider_capabilities;
