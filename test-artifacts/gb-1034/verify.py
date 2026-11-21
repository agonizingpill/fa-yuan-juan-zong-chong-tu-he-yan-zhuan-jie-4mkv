"""gb-1034 双方归一冲突核验与转介占位的端到端验证（SQLite 内存库 + FastAPI TestClient）。

运行：cd backend && python -I /workspace/test-artifacts/gb-1034/verify.py
不导入 worker/algorithm，避免 scikit-learn 依赖；主题分流直接落库模拟主管已复核。
"""
import os, sys, tempfile, types
_fd, _db_path = tempfile.mkstemp(suffix='.db'); os.close(_fd)
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_db_path}')
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6390/0')
os.environ.setdefault('SEED_DEMO', 'false')
os.environ.setdefault('JWT_SECRET', 'test-secret')
sys.path.insert(0, '/workspace/backend')

from sqlalchemy import select, create_engine
from sqlalchemy.exc import IntegrityError
from fastapi.testclient import TestClient

from src.database import Base, engine, Session
from src.models import (Intake, Consent, Party, CaseParty, ConflictCheck, Triage,
                        Requirement, Document, Agency, Referral, User)
from src.auth import password_hash
from src import conflict_service as cs
from src import referral_service as rs
from src import intake_service as ins
from src import closure_service as cls
from src.app import app

PASS = 0
def ok(name, cond, detail=''):
    global PASS
    assert cond, f'FAIL: {name} {detail}'
    PASS += 1
    print(f'  ok  {name}')

class U:
    def __init__(self, n): self.username = n
SUP, OP = U('supervisor'), U('operator')

Base.metadata.create_all(engine)

def session():
    return Session()

def make_case(code, client_name, opp_name, authorized=True, topic='labor'):
    db = session()
    case = Intake(code=code, alias=code + '来访', facts='模拟事实文本不少于八个字', status='open')
    db.add(case); db.flush()
    db.add(Consent(intake_id=case.id, authorized=authorized, basis='模拟授权存根'))
    ids = {}
    for side, nm in (('client', client_name), ('opponent', opp_name)):
        key = cs.normalize(nm)
        p = db.scalar(select(Party).where(Party.normalized == key))
        if not p:
            p = Party(name=nm.strip(), normalized=key); db.add(p); db.flush()
        db.add(CaseParty(intake_id=case.id, party_id=p.id, side=side)); ids[side] = p.id
    tr = Triage(intake_id=case.id, facts_version=1, status='reviewed', predicted=topic,
                confidence=0.9, approved=topic, manual_required=False, review_note='主管复核')
    db.add(tr)
    for req in db.scalars(select(Requirement).where(Requirement.topic == topic)):
        db.add(Document(intake_id=case.id, requirement_id=req.id, reference=code + '-脱敏材料.pdf',
                        status='verified', note='已核验'))
    db.commit(); db.close()
    return case.id

def make_agency(code, name, cap=2, specs='labor,housing'):
    db = session()
    a = Agency(code=code, name=name, specialties=specs, capacity=cap, active_count=0)
    db.add(a); db.flush(); db.commit(); aid = a.id; db.close()
    return aid

def party_input(intake_id, side, name):
    return types.SimpleNamespace(intake_id=intake_id, side=side, name=name)

def check_input(intake_id, agency_id):
    return types.SimpleNamespace(intake_id=intake_id, agency_id=agency_id)

def ref_input(intake_id, agency_id):
    return types.SimpleNamespace(intake_id=intake_id, agency_id=agency_id)

# ---------- 基础数据 ----------
db = session()
for topic, title in [('labor', '劳动报酬记录'), ('housing', '脱敏租赁合同'), ('consumer', '脱敏购物凭证')]:
    for code, name in [('identity', '脱敏身份授权页'), ('evidence', title)]:
        db.add(Requirement(topic=topic, code=code, title=name))
for role in ('operator', 'supervisor', 'viewer'):
    db.add(User(username=role, password=password_hash('Aid@2026'), role=role))
db.commit(); db.close()

yn = make_agency('YN', '友邻服务站', cap=2)
hm = make_agency('HM', '和睦接待室', cap=3)

print('1) 归一化口径')
ok('全角字母/数字 NFKC 折叠', cs.normalize('Ｑｉａｏ １２') == cs.normalize('qiao12'))
ok('空格/制表/换行全部去除', cs.normalize(' 青 桥\t商 行\n') == cs.normalize('青桥商行'))
ok('大小写折叠', cs.normalize('ABC') == cs.normalize('abc'))
ok('中文全角括号归一', cs.normalize('青桥（商行）') == cs.normalize('青桥(商行)'))

print('2) 带空格/全半角异写关联到同一 Party')
c1 = make_case('LZ100', '林某', '青桥商行')
db = session()
cs.party(db, party_input(c1, 'opponent', ' 青 桥 商 行 '), OP)
p1 = db.scalar(select(Party).where(Party.id == db.scalar(select(CaseParty).where(
    CaseParty.intake_id == c1, CaseParty.side == 'opponent')).party_id))
db.close()
c2 = make_case('LZ101', '青桥商行', '林某')   # 反向对立：同一批归一当事人
db = session()
n_parties = len(db.scalars(select(Party)).all()); db.close()
ok('异写名称复用同一 Party 行（仅 2 个当事人）', n_parties == 2)

print('3) 机构承接对立当事人 -> 冲突命中，且 basis 带命中卷宗与归一依据')
db = session()
r = rs.refer(db, ref_input(c1, yn), SUP)      # LZ100（林某 vs 青桥商行）发给友邻
db.close()
db = session()
blocked, reason, basis = cs.conflict(db, c2, yn)  # LZ101（青桥商行 vs 林某）对照友邻
ok('对立方向命中冲突', blocked is True)
ok('命中原因含卷宗号', 'LZ100' in reason, reason)
ok('basis.hits 记录命中卷宗与归一键',
   basis['hits'] and basis['hits'][0]['intake_code'] == 'LZ100'
   and basis['hits'][0]['normalized'] == '青桥商行')
ok('basis 含双方归一快照', basis['client']['normalized'] == '青桥商行'
   and basis['opponent']['normalized'] == '林某')
ok('opponent_load 列出机构当前在途对方',
   [x['intake_code'] for x in basis['opponent_load']] == ['LZ100'])
db.close()

print('4) 核验存根落库并保存 party_version 与依据')
db = session()
row = cs.check(db, check_input(c2, yn), OP)
ok('存根 blocked=True', row['blocked'] is True)
ok('存根记录核验版本', row['party_version'] == 1)
ok('存根 basis 持久化命中卷宗', row['basis']['hits'][0]['intake_code'] == 'LZ100')
check_id = row['id']; db.close()

print('5) 未命中机构可通过核验')
db = session()
blocked2, reason2, basis2 = cs.conflict(db, c2, hm)
ok('无承接机构放行', blocked2 is False and basis2['hits'] == [])
db.close()

print('6) 冲突存在时发信被拒，未命中才占位')
db = session()
try:
    rs.refer(db, ref_input(c2, yn), SUP); raised = False
except Exception as e:
    raised = '冲突' in str(e) or '对立' in str(e)
ok('命中冲突拒绝发信', raised)
db.rollback(); db.close()
db = session()
r2 = rs.refer(db, ref_input(c2, hm), SUP)
ok('未命中发信成功并占在途位', r2['status'] == 'pending' and r2['active_intake'] == c2)
agency_hm = db.get(Agency, hm)
ok('机构 active_count 增加', agency_hm.active_count == 1)
db.close()

print('7) 同卷只保留一条在途：服务层拦截 + 数据库可空唯一列双保险')
db = session()
try:
    rs.refer(db, ref_input(c2, yn), SUP); raised2 = False
except Exception as e:
    raised2 = '在途转介' in str(e)
ok('重复发信被服务层拒绝', raised2)
db.rollback(); db.close()
# 绕过服务层直接插第二条 active 行，验证数据库约束（模拟并发下锁竞争的兜底）
db = session()
dup = Referral(intake_id=c2, agency_id=yn, topic='labor', status='pending', active_intake=c2)
db.add(dup)
try:
    db.flush(); constrained = False
except IntegrityError:
    constrained = True; db.rollback()
ok('部分唯一索引阻止同卷第二条在途承接', constrained)
db.close()

print('8) 撤回旧转介后旧核验失效；重新核验按最新事实放行')
db = session()
decorated = cs.decorate_check(db, db.get(ConflictCheck, check_id))
ok('事实未变时旧核验仍 fresh', decorated['fresh'] is True)
db.close()
db = session()
ref1 = db.get(Referral, r['id'])
rs.withdraw(db, ref1.id, types.SimpleNamespace(note='主管撤回改派'), SUP)
ok('撤回退出在途唯一列', db.get(Referral, r['id']).active_intake is None)
ok('撤回释放承接位', db.get(Agency, yn).active_count == 0)
db.close()
db = session()
decorated2 = cs.decorate_check(db, db.get(ConflictCheck, check_id))
ok('撤回后旧核验标记失效', decorated2['fresh'] is False)
ok('失效存根附按最新事实复算结论', 'current_reason' in decorated2)
blocked3, _, _ = cs.conflict(db, c2, yn)
ok('撤回后实时核验放行', blocked3 is False)
db.close()

print('9) 新补录/更正相对方推进 party_version，迟到旧核验不放行')
c3 = make_case('LZ102', '周某', '旧相对方')
db = session()
chk = cs.check(db, check_input(c3, hm), OP); vid = chk['id']
db.close()
db = session()
cs.party(db, party_input(c3, 'opponent', '新相对方公司'), OP)
case3 = db.get(Intake, c3); ok('当事人更正推进版本', case3.party_version == 2)
db.close()
db = session()
dec = cs.decorate_check(db, db.get(ConflictCheck, vid))
ok('版本不符的旧核验判失效', dec['fresh'] is False)
db.close()
db = session()
try:
    cs.party(db, party_input(c3, 'client', '新相对方公司'), OP); same = False
except Exception as e:
    same = '同一当事人' in str(e)
ok('双方不得为同一当事人', same); db.rollback(); db.close()

print('10) 重试发信不重复占位：撤回后再发仅一条在途')
db = session()
r3 = rs.refer(db, ref_input(c1, yn), SUP)   # LZ100 之前的转介已撤回，可重新发
db.close()
db = session()
active = list(db.scalars(select(Referral).where(Referral.intake_id == c1, Referral.active_intake == c1)))
ok('重发后同卷仅一条在途', len(active) == 1 and active[0].id == r3['id'])
ok('重发只占一个承接位', db.get(Agency, yn).active_count == 1)
db.close()

print('11) 授权撤回自动终止在途并释放位置')
db = session()
ins.consent(db, c1, types.SimpleNamespace(authorized=False, basis='来访人撤回授权'), OP)
ok('授权撤回自动 withdrawn', db.get(Referral, r3['id']).status == 'withdrawn'
   and db.get(Referral, r3['id']).active_intake is None)
ok('卷宗回到 open 且容量归还', db.get(Intake, c1).status == 'open'
   and db.get(Agency, yn).active_count == 0)
db.close()
db = session()
try:
    rs.refer(db, ref_input(c1, yn), SUP); noauth = False
except Exception as e:
    noauth = '授权' in str(e)
ok('未授权禁止再次发信', noauth); db.rollback(); db.close()

print('12) 销案路径同样退出在途唯一列并释放承接位')
db = session()
# c2 的转介在 HM 处于 pending，先受理再造工时销案
rs.accept(db, r2['id'], types.SimpleNamespace(receipt='HM-ACK-101'), SUP)
from src.models import ServiceLog, Closure
db.add(ServiceLog(referral_id=r2['id'], minutes=45, description='材料沟通')); db.commit()
cls.close(db, types.SimpleNamespace(intake_id=c2, satisfaction=5, followup='回访完成'), SUP)
ok('销案转介 completed 且退出在途', db.get(Referral, r2['id']).status == 'completed'
   and db.get(Referral, r2['id']).active_intake is None)
ok('销案释放承接位', db.get(Agency, hm).active_count == 0)
db.close()

print('13) 机构名册与工作区 payload')
db = session()
roster = cs.agency_roster(db)
yn_rows = next(x for x in roster if x['agency_id'] == yn)
ok('名册只含在途承接（撤回/销案已移除）',
   all(it['intake_code'] != 'LZ100' for it in yn_rows['items']))
db.close()

print('14) API 级：登录、viewer 只读、workspace 字段')
with TestClient(app) as client:
    tok = client.post('/auth/login', json={'username': 'supervisor', 'password': 'Aid@2026'}).json()['token']
    h = {'Authorization': f'Bearer {tok}'}
    ws = client.get('/workspace', headers=h).json()
    ok('workspace 含 agency_roster', 'agency_roster' in ws and len(ws['agency_roster']) == 2)
    ok('workspace 冲突存根带 fresh/current_* 字段',
       all(('fresh' in x) for x in ws['conflict_checks']))
    vtok = client.post('/auth/login', json={'username': 'viewer', 'password': 'Aid@2026'}).json()['token']
    resp = client.post('/conflict-checks', headers={'Authorization': f'Bearer {vtok}'},
                       json={'intake_id': c3, 'agency_id': hm})
    ok('viewer 写接口被拒(403)', resp.status_code == 403)
    resp2 = client.post('/conflict-checks', headers=h, json={'intake_id': c3, 'agency_id': hm})
    ok('主管可建核验存根', resp2.status_code == 200 and resp2.json()['basis']['hits'] == [])

print('15) 在途唯一列在独立文件库上同样生效（跨 MySQL/SQLite/PG 可空唯一列语义）')
fd, path = tempfile.mkstemp(suffix='.db'); os.close(fd)
eng2 = create_engine(f'sqlite:///{path}')
Base.metadata.create_all(eng2)
from sqlalchemy.orm import Session as S2
with S2(eng2) as z:
    z.add(Intake(code='X1', alias='x', facts='模拟事实文本不少于八个字'))
    z.add(Agency(code='A1', name='a', specialties='labor', capacity=2, active_count=0)); z.commit()
    z.add(Referral(intake_id=1, agency_id=1, topic='labor', status='pending', active_intake=1)); z.commit()
    z.add(Referral(intake_id=1, agency_id=1, topic='labor', status='withdrawn', active_intake=None)); z.commit()
    try:
        z.add(Referral(intake_id=1, agency_id=1, topic='labor', status='pending', active_intake=1))
        z.commit(); idx_ok = False
    except IntegrityError:
        idx_ok = True; z.rollback()
    ok('同卷第二条在途被唯一约束拒绝，历史 withdrawn 行可共存', idx_ok)
os.remove(path)

print(f'\n全部 {PASS} 项断言通过 ✔')
engine.dispose()
os.remove(_db_path)
