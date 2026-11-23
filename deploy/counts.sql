-- Rows per table, run as the superuser (row-level security does not hide rows from it). The
-- restore compares them with counts.txt, which the backup reads back from its own dump
-- (app/hosted/backups.py count-rows prints the same lines in the same order).
SELECT format('%s %s', table_name,
              (xpath('/row/c/text()',
                     query_to_xml(format('SELECT count(*) AS c FROM public.%I', table_name),
                                  false, true, '')))[1]::text)
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name COLLATE "C";
