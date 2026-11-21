from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .models import Agency,Referral,Intake
from .constants import Topic
from .common import get,reject,finish,editable,lock_referral,active_referral,ACTIVE
from .intake_service import authorized
from .triage_service import latest
from .document_service import missing
from .conflict_service import conflict

def agency(db,data,user):
    reject(any(s not in {t.value for t in Topic} for s in data.specialties),'专长必须为劳动、住房或消费主题')
    reject(db.scalar(select(Agency).where(Agency.code==data.code)) is not None,'机构编号重复')
    row=Agency(code=data.code,name=data.name,specialties=','.join(sorted(set(data.specialties))),capacity=data.capacity,active_count=0);db.add(row);db.flush();return finish(db,user,'登记合作机构',row)

def refer(db,data,user):
    # 固定锁序：先 Intake 根锁，再 Agency 锁，全部容量与在途判断都在两锁内完成
    case=editable(db,data.intake_id);agency=get(db,Agency,data.agency_id,True)
    reject(active_referral(db,case.id) is not None,'存在在途转介，请先撤回未受理转介')
    reject(not authorized(db,case.id),'授权未取得或已撤回，禁止转介')
    triage=latest(db,case.id);reject(not triage,'主题尚未人工复核，低置信结果必须退回人工')
    reject(triage.approved not in agency.specialties.split(','),'机构专长不覆盖该咨询主题')
    absent=missing(db,case.id,triage.approved);reject(bool(absent),'缺少关键材料：'+','.join(absent))
    # 冲突与容量均以机构锁内读到的最新已提交事实为准，迟到的旧核验不能放行
    blocked,reason,_=conflict(db,case.id,agency.id);reject(blocked,reason)
    reject(agency.active_count>=agency.capacity,'机构当前承接已满，可撤回未受理转介后改派')
    row=Referral(intake_id=case.id,agency_id=agency.id,topic=triage.approved,status='pending',active_intake=case.id)
    db.add(row)
    try:db.flush()
    except IntegrityError:
        # 并发下另一个事务已抢先占位：唯一约束兜底，拒绝重复承接
        db.rollback();raise HTTPException(409,'存在在途转介，请先撤回未受理转介')
    agency.active_count+=1;case.status='referred'
    return finish(db,user,'发出转介信',row)

def accept(db,id,data,user):
    row=lock_referral(db,id);case=get(db,Intake,row.intake_id,True);get(db,Agency,row.agency_id,True)
    reject(row.status!='pending','仅待受理转介可登记回执')
    reject(not authorized(db,case.id),'授权已撤回，不可受理')
    blocked,reason,_=conflict(db,case.id,row.agency_id);reject(blocked,reason)
    row.status='accepted';row.receipt=data.receipt;case.status='accepted';return finish(db,user,'录入机构受理回执',row)

def withdraw(db,id,data,user):
    row=lock_referral(db,id);case=get(db,Intake,row.intake_id,True);agency=get(db,Agency,row.agency_id,True)
    reject(row.status not in ACTIVE,'当前转介不能撤回')
    # 主管可中止已受理承接，保留服务历史。撤回后重新授权/补正才可另派。
    row.status='withdrawn';row.note=data.note;row.active_intake=None;agency.active_count-=1;case.status='open'
    return finish(db,user,'撤回或中止承接',row)
