from waive.atlas import repo
from waive.db import CaseRow, init_db, make_engine, session_scope
from waive.demo import forget_cases, seed_demo


def test_seed_and_forget():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with session_scope(engine) as session:
        seed_demo(session)
        seed_demo(session)
        assert repo.latest_sheet(session, "229999")[0].version == 1
        session.add(CaseRow(id="x", state="MA", status="new", token_generation=1))
    with session_scope(engine) as session:
        assert forget_cases(session) == 1
