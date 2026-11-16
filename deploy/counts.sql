-- Rows per table, run as the superuser (row-level security does not hide rows from it). The
-- backup records these; the restore compares them with the restored database.
SELECT format('%s %s', table_name,
              (xpath('/row/c/text()',
                     query_to_xml(format('SELECT count(*) AS c FROM public.%I', table_name),
                                  false, true, '')))[1]::text)
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name;
