-- Historical grant attempt: the platform-owned pg_net objects can retain their
-- public grants despite these statements. Do not rely on this as a boundary.
-- The following private_http_dispatcher migration eliminates credential-bearing
-- pg_net requests, and operator rollout rotates/revokes the former credential.
grant usage on schema net to postgres;
grant all on all tables in schema net to postgres;
grant all on all sequences in schema net to postgres;
grant execute on all functions in schema net to postgres;
revoke all on schema net from public,anon,authenticated,service_role;
revoke all on all tables in schema net from public,anon,authenticated,service_role;
revoke all on all sequences in schema net from public,anon,authenticated,service_role;
revoke all on all functions in schema net from public,anon,authenticated,service_role;
