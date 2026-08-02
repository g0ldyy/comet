from databases.backends.sqlite import SQLiteTransaction

_sqlite_transaction_start = SQLiteTransaction.start


async def _start_transaction(self, is_root, extra_options):
    if is_root and extra_options.get("sqlite_begin_immediate", False):
        assert self._connection._connection is not None
        self._is_root = True
        async with self._connection._connection.execute("BEGIN IMMEDIATE") as cursor:
            await cursor.close()
        return
    await _sqlite_transaction_start(self, is_root, extra_options)


SQLiteTransaction.start = _start_transaction


def write_transaction(database, *args, **kwargs):
    if str(database.url).startswith("sqlite"):
        kwargs["sqlite_begin_immediate"] = True
    return database.transaction(*args, **kwargs)
