import unicodedata
from sqlalchemy import select
from .models import Party,CaseParty,ConflictCheck,Agency,Referral,Intake
from .common import get,reject,finish,editable,ACTIVE

def normalize(name):
    # NFKC 全半角归一、大小写折叠并去除全部空白，保证“青 桥 商行 / 青橋商行 / QIAOXIAO”可归到同一当事人
    return ''.join(unicodedata.normalize('NFKC',name).casefold().split())

def party_sides(db,intake_id):
    return {r.side:r.party_id for r in db.scalars(select(CaseParty).where(CaseParty.intake_id==intake_id))}

def party(db,data,user):
    case=editable(db,data.intake_id)
    name=normalize(data.name);reject(len(name)<2,'归一化当事人名称过短')
    p=db.scalar(select(Party).where(Party.normalized==name))
    if not p:
        p=Party(name=data.name.strip(),normalized=name);db.add(p);db.flush()
    opposite=db.scalar(select(CaseParty).where(CaseParty.intake_id==data.intake_id,CaseParty.side!=data.side,CaseParty.party_id==p.id))
    reject(opposite is not None,'双方不能是同一当事人')
    row=db.scalar(select(CaseParty).where(CaseParty.intake_id==data.intake_id,CaseParty.side==data.side))
    if row:
        if row.party_id!=p.id:
            row.party_id=p.id;case.party_version+=1
    else:
        row=CaseParty(intake_id=data.intake_id,side=data.side,party_id=p.id);db.add(row);case.party_version+=1
    db.flush()
    return finish(db,user,'关联当事人更正',row)

def conflict(db,intake_id,agency_id):
    """按当前已提交事实实时对照机构在途承接。返回 (blocked, reason, basis)。"""
    parties=party_sides(db,intake_id)
    # 机构行锁由写调用方（check/refer/accept）在进入前持有；此处只做存在性读取，避免 GET /workspace 装饰存根时误加写锁
    get(db,Agency,agency_id)
    if set(parties)!=set(['client','opponent']):
        return True,'需补齐来访方与相对方',{'hits':[],'opponent_load':[]}
    party_rows={p.id:p for p in db.scalars(select(Party).where(Party.id.in_(parties.values())))}
    refs=list(db.scalars(select(Referral).where(Referral.agency_id==agency_id,Referral.intake_id!=intake_id,Referral.status.in_(ACTIVE))))
    hits=[];opponent_load=[]
    for ref in refs:
        sides=party_sides(db,ref.intake_id)
        other_case=get(db,Intake,ref.intake_id)
        if 'client' in sides:
            other_client=db.get(Party,sides['client'])
            opponent_load.append({'intake_code':other_case.code,'party_name':other_client.name if other_client else '',
                                  'normalized':other_client.normalized if other_client else '',
                                  'side':'client','referral_id':ref.id,'status':ref.status})
        hit_party=None
        if parties['client']==sides.get('opponent'):
            hit_party=db.get(Party,parties['client'])
        elif parties['opponent']==sides.get('client'):
            hit_party=db.get(Party,parties['opponent'])
        if hit_party is not None:
            hits.append({'intake_code':other_case.code,'referral_id':ref.id,'status':ref.status,
                         'party_name':hit_party.name,'normalized':hit_party.normalized})
    basis={'client':{'name':party_rows[parties['client']].name,'normalized':party_rows[parties['client']].normalized},
           'opponent':{'name':party_rows[parties['opponent']].name,'normalized':party_rows[parties['opponent']].normalized},
           'opponent_load':opponent_load,'hits':hits}
    if hits:
        codes='、'.join(sorted({h['intake_code'] for h in hits}))
        return True,f'机构已承接对立当事人（命中卷宗 {codes}），改选其它机构',basis
    basis['reason']='双方对照通过；转介时再次核验'
    return False,basis['reason'],basis

def check(db,data,user):
    case=get(db,Intake,data.intake_id,True);get(db,Agency,data.agency_id,True)
    blocked,reason,basis=conflict(db,data.intake_id,data.agency_id)
    row=ConflictCheck(intake_id=data.intake_id,agency_id=data.agency_id,blocked=blocked,reason=reason,
                      party_version=case.party_version,basis=basis)
    db.add(row);db.flush()
    return finish(db,user,'机构利益冲突核验',row)

def agency_fingerprint(db,agency_id):
    """机构当前在途承接中相对方归一键集合，用于判断旧核验是否被新事实推翻。"""
    refs=list(db.scalars(select(Referral).where(Referral.agency_id==agency_id,Referral.status.in_(ACTIVE))))
    keys=set()
    for ref in refs:
        sides=party_sides(db,ref.intake_id)
        if 'client' in sides:
            p=db.get(Party,sides['client'])
            if p:keys.add(p.normalized)
    return frozenset(keys)

def agency_roster(db):
    """各机构当前在途承接的对方名册，供主管发信前直接核对。"""
    out=[]
    for agency in db.scalars(select(Agency).order_by(Agency.id)):
        items=[]
        refs=list(db.scalars(select(Referral).where(Referral.agency_id==agency.id,Referral.status.in_(ACTIVE)).order_by(Referral.id)))
        for ref in refs:
            case=get(db,Intake,ref.intake_id)
            sides=party_sides(db,ref.intake_id)
            for side in ('client','opponent'):
                p=db.get(Party,sides[side]) if side in sides else None
                if p:
                    items.append({'intake_code':case.code,'party_name':p.name,'normalized':p.normalized,
                                  'side':side,'referral_id':ref.id,'status':ref.status})
        out.append({'agency_id':agency.id,'agency_name':agency.name,'items':items})
    return out

def decorate_check(db,row):
    """给历史核验存根附加实时有效性：当事人补录/撤回旧转介后，迟到的旧核验标记为失效。"""
    data={c.name:getattr(row,c.name) for c in row.__table__.columns}
    case=db.get(Intake,row.intake_id)
    basis=data.get('basis') or {}
    stale_party=case is not None and data.get('party_version')!=case.party_version
    stale_load=agency_fingerprint(db,row.agency_id)!=frozenset(x.get('normalized') for x in basis.get('opponent_load',[]))
    data['fresh']=not (stale_party or stale_load)
    if not data['fresh']:
        blocked,reason,live_basis=conflict(db,row.intake_id,row.agency_id)
        data['current_blocked']=blocked;data['current_reason']=reason;data['current_basis']=live_basis
    return data

