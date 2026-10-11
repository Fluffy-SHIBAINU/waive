import sqlite3
import threading
import time

from sqlalchemy import text

from waive.db import SQLITE_BUSY_SECONDS, make_engine


def test_a_file_database_waits_for_the_other_writer_and_lets_readers_through(tmp_path):
    # 2026-10-11: confirming a bill in the web app failed with "database is locked" while a
    # scouting batch was writing to the same file.
    engine = make_engine(f"sqlite:///{tmp_path / 'var' / 'waive.db'}")
    try:
        with engine.connect() as connection:
            assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
            busy_ms = connection.execute(text("PRAGMA busy_timeout")).scalar()
            assert busy_ms == SQLITE_BUSY_SECONDS * 1000 and SQLITE_BUSY_SECONDS >= 30
    finally:
        engine.dispose()


def test_a_second_writer_waits_instead_of_failing(tmp_path):
    path = tmp_path / "waive.db"
    engine = make_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as setup:
            setup.execute(text("CREATE TABLE notes (body TEXT)"))
        batch = sqlite3.connect(path, check_same_thread=False)
        batch.execute("BEGIN IMMEDIATE")  # the batch: a write lock held for a while
        batch.execute("INSERT INTO notes VALUES ('batch')")

        def release():
            time.sleep(0.3)
            batch.commit()

        thread = threading.Thread(target=release)
        thread.start()
        with engine.begin() as web:  # the web app waits its turn; it does not raise
            web.execute(text("INSERT INTO notes VALUES ('case')"))
        thread.join()
        batch.close()
        with engine.connect() as reader:
            assert reader.execute(text("SELECT count(*) FROM notes")).scalar() == 2
    finally:
        engine.dispose()


def test_a_database_another_process_is_writing_to_can_still_be_opened(tmp_path):
    # The first version of the fix switched to write-ahead logging on every connect; SQLite
    # refuses that switch at once while another connection is mid-write, and `waive serve`
    # would not start during a scouting batch.
    path = tmp_path / "waive.db"
    batch = sqlite3.connect(path, check_same_thread=False)  # an old process, rollback journal
    batch.execute("CREATE TABLE notes (body TEXT)")
    batch.commit()
    batch.execute("BEGIN IMMEDIATE")
    batch.execute("INSERT INTO notes VALUES ('batch')")
    engine = make_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as reader:  # connecting must not raise
            assert (
                reader.execute(text("PRAGMA busy_timeout")).scalar() == SQLITE_BUSY_SECONDS * 1000
            )
        threading.Timer(0.3, batch.commit).start()
        with engine.begin() as web:  # and a write waits for the batch
            web.execute(text("INSERT INTO notes VALUES ('case')"))
        with engine.connect() as reader:
            assert reader.execute(text("SELECT count(*) FROM notes")).scalar() == 2
    finally:
        engine.dispose()
        batch.close()
