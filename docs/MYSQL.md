# Optional MySQL storage

SQLite remains the default. MySQL 8.0/8.4 can store task snapshots, events,
operation claims/results, and encrypted approval requests. It does not turn
the local background-worker queue into a distributed scheduler. This adapter
does not migrate existing SQLite tasks or import another application's tables.

Install the optional dependencies in your project environment:

```sh
python -m pip install -e '.[mysql]'
```

The extra requires SQLAlchemy 2.x and PyMySQL 1.2 or newer. The latter's
explicit TLS mode rejects servers that do not support TLS when CA settings
are provided. Do not lower this dependency floor.

## Configuration

Use a dedicated database created for ForgePilot, with permission to create
tables and read/write its own data. Never point test runs at a production
database. Passwords belong in the process environment, not TOML or a DSN:

```toml
[storage]
backend = "mysql"

[storage.mysql]
host = "127.0.0.1"
port = 13306
database = "forgepilot_test"
username = "forgepilot"
password_env = "FORGEPILOT_TEST_MYSQL_PASSWORD"
# Required for remote hosts; verifies the certificate AND host identity:
# tls_ca = "/absolute/path/to/mysql-ca.pem"
```

The local Python API is `MySQLStore(MySQLConfig(...), key_path=Path(...))`.
Every process sharing one database must use the same approval encryption key.
Keep that local key with owner-only permissions (0600), and back it up
separately from the database. Losing the key makes pending approvals
unrecoverable; it does not prevent reading ordinary events. The constructor
creates the key when missing and refuses permissive key files. `close()`
disposes the connection pool.

Only literal loopback IPs and `localhost` may omit TLS. Remote configurations
must provide a CA file. Certificate and hostname verification are both enabled.
Database errors are surfaced without SQL parameter values or driver messages.
The browser never receives the database password or approval key.

## Disposable local MySQL

Docker is optional; installing or starting Docker/MySQL is an explicit operator
action. The compose file binds only `127.0.0.1`. Set two different disposable
passwords in your shell using your usual secure environment mechanism, then run:

```sh
export FORGEPILOT_TEST_MYSQL_HOST=127.0.0.1
export FORGEPILOT_TEST_MYSQL_PORT=13306
export FORGEPILOT_TEST_MYSQL_DATABASE=forgepilot_test
export FORGEPILOT_TEST_MYSQL_USERNAME=forgepilot
# Set FORGEPILOT_TEST_MYSQL_PASSWORD and FORGEPILOT_TEST_MYSQL_ROOT_PASSWORD.
docker compose -f compose.mysql.yml up -d --wait
export FORGEPILOT_TEST_MYSQL_ENABLE=1
python -m pytest tests/integration/test_mysql_store.py -q
```

For a remote dedicated test database also set `FORGEPILOT_TEST_MYSQL_TLS_CA`.
All five connection variables and `ENABLE=1` are required for real tests.
An explicitly enabled but incomplete configuration fails instead of silently
skipping. Tests create unique `mysql-test-*` tasks and leave them for inspection;
they do not delete existing tables or data.

Stop this disposable service while retaining its data:

```sh
docker compose -f compose.mysql.yml down
```

Only if you want to erase this demo's database, `docker compose -f
compose.mysql.yml down --volumes` deletes its named volume permanently.

## Transaction guarantees and verification boundary

All task mutations take `SELECT ... FOR UPDATE` on the existing task row
inside one transaction. Events, sequence validation, snapshots, approval
changes, and operation claims/results commit or roll back together. Unique
constraints backstop event and operation identity. InnoDB, `READ COMMITTED`,
UTF-8, and case-sensitive binary identity collation are explicit. A failed
claim cannot authorize a second execution. Recommitting an already stored
operation preserves its original result while allowing the same replay-event
behavior as the SQLite adapter. Failed or ambiguous commits are surfaced;
there is no automatic retry that could re-execute a tool.

The default tests execute shared persistence logic against an actual SQLite
database through SQLAlchemy. They cover rollback, restart, sequence cursors,
approval encryption, claim conflicts, and committed-result replay. These
checks do **not** verify MySQL protocol, InnoDB locks, TLS, or MySQL DDL.
Opt-in MySQL tests run those same contracts and concurrent append/claim tests
against the configured server. A skipped MySQL test is not MySQL acceptance.

References: [SQLAlchemy MySQL dialect](https://docs.sqlalchemy.org/en/20/dialects/mysql.html),
[MySQL locking reads](https://dev.mysql.com/doc/refman/8.0/en/innodb-locking-reads.html),
[PyMySQL connection options](https://pymysql.readthedocs.io/en/latest/modules/connections.html).
