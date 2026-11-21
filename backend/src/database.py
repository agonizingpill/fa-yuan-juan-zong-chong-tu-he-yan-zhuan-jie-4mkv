import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
_url=os.environ['DATABASE_URL']
# MySQL/PostgreSQL 采用 READ COMMITTED，容量锁内查询可看到已提交的最新转介；SQLite 不支持该级别，用其默认隔离
engine=create_engine(_url,pool_pre_ping=True,isolation_level=None if _url.startswith('sqlite') else "READ COMMITTED")
Session=sessionmaker(engine,expire_on_commit=False)
class Base(DeclarativeBase):pass
def session():
    with Session() as db:
        try:yield db
        except Exception:db.rollback();raise
