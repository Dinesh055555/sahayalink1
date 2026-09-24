"""SahayaLink backend — FastAPI + SQLite. Region-scoped worklist, action logging,
incentive points, unreachable-reassignment, supervisor view."""
import sqlite3, os, hashlib, secrets, json
from datetime import date, timedelta, datetime
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, 'sahayalink.db')
app = FastAPI(title='SahayaLink API')

TOKENS = {}  # token -> user_id  (in-memory session store; fine for the demo)

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def pin(s): return hashlib.sha256(s.encode()).hexdigest()

def user_from_token(authorization: str = Header(None)):
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Not logged in')
    tok = authorization.split(' ', 1)[1]
    uid = TOKENS.get(tok)
    if not uid:
        raise HTTPException(401, 'Session expired, log in again')
    con = db(); u = con.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone(); con.close()
    if not u: raise HTTPException(401, 'Unknown user')
    return dict(u)

# ---------- auth ----------
class Login(BaseModel):
    username: str
    pin: str

@app.post('/api/login')
def login(body: Login):
    con = db()
    u = con.execute('SELECT * FROM users WHERE username=?', (body.username.strip(),)).fetchone()
    con.close()
    if not u or u['pin_hash'] != pin(body.pin.strip()):
        raise HTTPException(401, 'Wrong username or PIN')
    tok = secrets.token_urlsafe(24)
    TOKENS[tok] = u['id']
    return {'token': tok, 'name': u['name'], 'role': u['role'],
            'district': u['district'], 'block': u['block']}

# ---------- worklist ----------
@app.get('/api/worklist')
def worklist(user=Depends(user_from_token)):
    con = db()
    today = date.today().isoformat()
    rows = con.execute('''SELECT * FROM worklist
        WHERE assigned_user_id=? AND work_date=?
        ORDER BY CASE priority WHEN 'Critical' THEN 0 ELSE 1 END, risk_pct DESC''',
        (user['id'], today)).fetchall()
    con.close()
    order = {'pending':0,'unreachable':1,'escalated':2,'resolved':3}
    out = sorted([dict(r) for r in rows], key=lambda r: (order.get(r['status'],9),))
    return {'date': today, 'count': len(out), 'cases': out}

@app.get('/api/stats')
def my_stats(user=Depends(user_from_token)):
    con = db(); today = date.today().isoformat()
    w = con.execute('''SELECT status, COUNT(*) n FROM worklist
        WHERE assigned_user_id=? AND work_date=? GROUP BY status''', (user['id'], today)).fetchall()
    counts = {r['status']: r['n'] for r in w}
    pts = con.execute('SELECT COALESCE(SUM(points),0) p FROM points_ledger WHERE user_id=?', (user['id'],)).fetchone()['p']
    con.close()
    tier = 'Gold' if pts>=120 else 'Silver' if pts>=50 else 'Bronze'
    total = sum(counts.values())
    resolved = counts.get('resolved',0)
    return {'total':total,'pending':counts.get('pending',0),'resolved':resolved,
            'unreachable':counts.get('unreachable',0),'escalated':counts.get('escalated',0),
            'points':pts,'tier':tier,
            'progress': round(100*resolved/total) if total else 0}

# ---------- log an action ----------
class Action(BaseModel):
    worklist_id: int
    outcome: str   # resolved | unreachable | escalate
    note: str = ''

POINTS = None
def points_map():
    global POINTS
    if POINTS is None:
        con = db(); POINTS = {r['rule']: r['points'] for r in con.execute('SELECT * FROM point_rules')}; con.close()
    return POINTS

@app.post('/api/action')
def log_action(body: Action, user=Depends(user_from_token)):
    con = db()
    w = con.execute('SELECT * FROM worklist WHERE id=?', (body.worklist_id,)).fetchone()
    if not w: con.close(); raise HTTPException(404,'Case not found')
    if w['assigned_user_id'] != user['id']:
        con.close(); raise HTTPException(403, 'This case is not on your list')
    pm = points_map(); now = datetime.now().isoformat(timespec='seconds')
    awarded = 0
    if body.outcome == 'resolved':
        base = pm['resolve_critical'] if w['priority']=='Critical' else pm['resolve_high'] if w['priority']=='High' else pm['resolve_other']
        awarded = base + (pm['hard_bonus'] if w['is_hard'] else 0)
        con.execute('UPDATE worklist SET status=? WHERE id=?', ('resolved', w['id']))
    elif body.outcome == 'unreachable':
        awarded = pm['attempt']
        con.execute('UPDATE worklist SET status=?, attempt_count=attempt_count+1 WHERE id=?',
                    ('unreachable', w['id']))
    elif body.outcome == 'escalate':
        con.execute('UPDATE worklist SET status=? WHERE id=?', ('escalated', w['id']))
    else:
        con.close(); raise HTTPException(400,'Unknown outcome')
    con.execute('INSERT INTO action_log(worklist_id,user_id,outcome,note,points_awarded,ts) VALUES(?,?,?,?,?,?)',
                (w['id'], user['id'], body.outcome, body.note, awarded, now))
    if awarded:
        con.execute('INSERT INTO points_ledger(user_id,points,reason,ts) VALUES(?,?,?,?)',
                    (user['id'], awarded, body.outcome, now))
    con.commit(); con.close()
    return {'ok':True, 'points_awarded':awarded}

# ---------- leaderboard ----------
@app.get('/api/leaderboard')
def leaderboard(user=Depends(user_from_token)):
    con = db()
    rows = con.execute('''SELECT u.name, u.block, u.district, COALESCE(SUM(p.points),0) pts
        FROM users u LEFT JOIN points_ledger p ON p.user_id=u.id
        WHERE u.role='ASHA' GROUP BY u.id ORDER BY pts DESC LIMIT 10''').fetchall()
    con.close()
    out=[]
    for i,r in enumerate(rows):
        pts=r['pts']; tier='Gold' if pts>=120 else 'Silver' if pts>=50 else 'Bronze'
        out.append({'rank':i+1,'name':r['name'],'block':r['block'],'pts':pts,'tier':tier})
    return {'leaders':out}

# ---------- supervisor ----------
@app.get('/api/supervisor/summary')
def supervisor(user=Depends(user_from_token)):
    if user['role'] not in ('SUPERVISOR','CHO'):
        raise HTTPException(403, 'Supervisor access only')
    con = db(); today=date.today().isoformat()
    by_status = {r['status']:r['n'] for r in con.execute(
        'SELECT status,COUNT(*) n FROM worklist WHERE work_date=? GROUP BY status',(today,))}
    by_village = con.execute('''SELECT village, district, COUNT(*) total,
        SUM(status='resolved') resolved, SUM(status='pending') pending,
        SUM(status IN ('unreachable','escalated')) stuck, ROUND(AVG(risk_pct)) avg_risk
        FROM worklist WHERE work_date=? GROUP BY village ORDER BY stuck DESC, total DESC LIMIT 20''',(today,)).fetchall()
    by_worker = con.execute('''SELECT u.name,u.block,COUNT(w.id) total,
        SUM(w.status='resolved') resolved
        FROM users u JOIN worklist w ON w.assigned_user_id=u.id AND w.work_date=?
        WHERE u.role='ASHA' GROUP BY u.id ORDER BY resolved DESC''',(today,)).fetchall()
    con.close()
    total=sum(by_status.values()); resolved=by_status.get('resolved',0)
    return {'date':today,'total':total,'resolved':resolved,
            'pending':by_status.get('pending',0),
            'stuck':by_status.get('unreachable',0)+by_status.get('escalated',0),
            'resolution_rate':round(100*resolved/total) if total else 0,
            'villages':[dict(r) for r in by_village],
            'workers':[dict(r) for r in by_worker]}

# ---------- nightly refresh (demo trigger) ----------
@app.post('/api/admin/run-nightly')
def run_nightly(user=Depends(user_from_token)):
    """Reassign unreachable cases to a different worker for tomorrow; escalate after 3 attempts."""
    if user['role'] not in ('SUPERVISOR','CHO'):
        raise HTTPException(403,'Admin only')
    con = db(); today=date.today().isoformat(); tomorrow=(date.today()+timedelta(days=1)).isoformat()
    moved=0; escalated=0
    stuck = con.execute("SELECT * FROM worklist WHERE work_date=? AND status='unreachable'",(today,)).fetchall()
    for w in stuck:
        if w['attempt_count'] >= 3:
            # escalate to a CHO covering that village
            cho = con.execute('''SELECT u.id FROM users u JOIN worker_villages wv ON wv.user_id=u.id
                WHERE u.role='CHO' AND wv.village=? LIMIT 1''',(w['village'],)).fetchone()
            target = cho['id'] if cho else w['assigned_user_id']; cadre='CHO'; escalated+=1
        else:
            # find a DIFFERENT ASHA covering the same village
            alt = con.execute('''SELECT u.id FROM users u JOIN worker_villages wv ON wv.user_id=u.id
                WHERE u.role='ASHA' AND wv.village=? AND u.id!=? LIMIT 1''',
                (w['village'], w['assigned_user_id'])).fetchone()
            target = alt['id'] if alt else w['assigned_user_id']; cadre=w['assigned_cadre']; moved+=1
        con.execute('''INSERT INTO worklist(work_date,episode_id,patient_ref,village,district,priority,
            risk_pct,dropout_stage,reason,recommended_action,assigned_cadre,assigned_user_id,distance_km,
            mobile_connectivity,diagnosis_group,preferred_language,age,gender,facility_name,attempt_count,status,is_hard)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (tomorrow,w['episode_id'],w['patient_ref'],w['village'],w['district'],w['priority'],
             w['risk_pct'],w['dropout_stage'],w['reason'],w['recommended_action'],cadre,target,
             w['distance_km'],w['mobile_connectivity'],w['diagnosis_group'],w['preferred_language'],
             w['age'],w['gender'],w['facility_name'],w['attempt_count'],
             'escalated' if cadre=='CHO' and escalated else 'pending',w['is_hard']))
    con.commit(); con.close()
    return {'ok':True,'reassigned':moved,'escalated':escalated,'for_date':tomorrow}

# ---------- hotspot map data (scoped) ----------
@app.get('/api/hotspots')
def hotspots(user=Depends(user_from_token)):
    con = db(); today=date.today().isoformat()
    scope = '' if user['role'] in ('SUPERVISOR','CHO') else \
        'AND w.village IN (SELECT village FROM worker_villages WHERE user_id=%d)' % user['id']
    rows = con.execute(f'''SELECT w.village, v.lat, v.lng, COUNT(*) cases,
        ROUND(AVG(w.risk_pct)) avg_risk, SUM(w.priority='Critical') critical
        FROM worklist w JOIN villages v ON v.village=w.village
        WHERE w.work_date=? {scope} AND v.lat IS NOT NULL
        GROUP BY w.village''',(today,)).fetchall()
    con.close()
    return {'hotspots':[dict(r) for r in rows]}

# ---------- workers & region management ----------
@app.get('/api/supervisor/workers')
def list_workers(user=Depends(user_from_token)):
    if user['role'] not in ('SUPERVISOR','CHO'):
        raise HTTPException(403,'Supervisor access only')
    con=db(); today=date.today().isoformat()
    rows=con.execute('''SELECT u.id,u.name,u.username,u.role,u.district,u.block,
        (SELECT GROUP_CONCAT(village,', ') FROM worker_villages WHERE user_id=u.id) villages,
        (SELECT COUNT(*) FROM worklist w WHERE w.assigned_user_id=u.id AND w.work_date=?) load_today,
        (SELECT COUNT(*) FROM worklist w WHERE w.assigned_user_id=u.id AND w.work_date=? AND w.status='resolved') resolved_today,
        (SELECT COALESCE(SUM(points),0) FROM points_ledger WHERE user_id=u.id) points
        FROM users u WHERE u.role IN ('ASHA','CHO') ORDER BY u.role, u.district, u.block, u.name''',
        (today,today)).fetchall()
    con.close()
    return {'workers':[dict(r) for r in rows]}

@app.get('/api/supervisor/regions')
def list_regions(user=Depends(user_from_token)):
    if user['role'] not in ('SUPERVISOR','CHO'):
        raise HTTPException(403,'Supervisor access only')
    con=db()
    rows=con.execute('''SELECT v.village,v.district,v.block,v.mobile_connectivity,
        (SELECT GROUP_CONCAT(u.name,', ') FROM worker_villages wv JOIN users u ON u.id=wv.user_id
         WHERE wv.village=v.village AND u.role='ASHA') ashas
        FROM villages v ORDER BY v.district,v.block,v.village''').fetchall()
    ashas=con.execute("SELECT id,name,block,district FROM users WHERE role='ASHA' ORDER BY district,block,name").fetchall()
    con.close()
    return {'regions':[dict(r) for r in rows],'ashas':[dict(r) for r in ashas]}

class Reassign(BaseModel):
    village: str
    from_user_id: int
    to_user_id: int
    for_date: str = ''   # blank = today

@app.post('/api/supervisor/reassign-village')
def reassign_village(body: Reassign, user=Depends(user_from_token)):
    if user['role'] not in ('SUPERVISOR','CHO'):
        raise HTTPException(403,'Supervisor access only')
    con=db(); d=body.for_date or date.today().isoformat()
    # move that village's pending cases to the new worker
    n=con.execute('''UPDATE worklist SET assigned_user_id=? WHERE village=? AND work_date=?
        AND assigned_user_id=? AND status='pending' ''',
        (body.to_user_id,body.village,d,body.from_user_id)).rowcount
    # ensure the new worker covers the village going forward
    con.execute('INSERT OR IGNORE INTO worker_villages(user_id,village) VALUES(?,?)',(body.to_user_id,body.village))
    con.commit(); con.close()
    return {'ok':True,'moved':n}

# ---------- download a worker's day-end report ----------
@app.get('/api/supervisor/worker-report/{worker_id}')
def worker_report(worker_id:int, user=Depends(user_from_token)):
    if user['role'] not in ('SUPERVISOR','CHO'):
        raise HTTPException(403,'Supervisor access only')
    con=db(); today=date.today().isoformat()
    rows=con.execute('''SELECT episode_id,patient_ref,village,district,priority,risk_pct,dropout_stage,
        reason,recommended_action,assigned_cadre,attempt_count,status
        FROM worklist WHERE assigned_user_id=? AND work_date=? ORDER BY status,priority''',
        (worker_id,today)).fetchall()
    con.close()
    import io,csv as _csv
    buf=io.StringIO(); w=_csv.writer(buf)
    cols=['episode_id','patient_ref','village','district','priority','risk_pct','dropout_stage',
          'reason','recommended_action','assigned_cadre','attempt_count','status']
    w.writerow(cols)
    for r in rows: w.writerow([r[c] for c in cols])
    from fastapi.responses import Response
    return Response(content=buf.getvalue(),media_type='text/csv',
        headers={'Content-Disposition':f'attachment; filename=worker_{worker_id}_report_{today}.csv'})

# ---------- upload next-day worklist (CSV) ----------
@app.get('/api/supervisor/upload-template')
def upload_template(user=Depends(user_from_token)):
    from fastapi.responses import Response
    cols='patient_ref,village,priority,risk_pct,dropout_stage,reason,recommended_action\n'
    sample='P001234,Devarakonda,Critical,95,Medicine not collected,no medicine pickup; far from facility,Call + confirm medicine pickup\n'
    return Response(content=cols+sample,media_type='text/csv',
        headers={'Content-Disposition':'attachment; filename=sahayalink_upload_template.csv'})

@app.post('/api/supervisor/upload-nextday')
async def upload_nextday(user=Depends(user_from_token)):
    raise HTTPException(400,'Use the multipart endpoint')

from fastapi import UploadFile, File, Form
@app.post('/api/supervisor/upload')
async def upload_worklist(file: UploadFile = File(...), for_date: str = Form(''),
                          user=Depends(user_from_token)):
    if user['role'] not in ('SUPERVISOR','CHO'):
        raise HTTPException(403,'Supervisor access only')
    import io,csv as _csv,re
    raw=(await file.read()).decode('utf-8-sig',errors='ignore')
    rdr=_csv.DictReader(io.StringIO(raw))
    d=for_date or (date.today()+timedelta(days=1)).isoformat()
    con=db()
    def nv(x): return re.sub(r'\s+',' ',re.sub(r'[^a-z0-9\s]',' ',str(x).lower())).strip() if x else ''
    # map normalized village -> canonical + primary ASHA
    vmap={}
    for v in con.execute('SELECT village FROM villages'):
        vmap[nv(v['village'])]=v['village']
    placed=0; skipped=0; per_worker={}
    for row in rdr:
        vil_in=row.get('village') or row.get('Village') or ''
        cv=vmap.get(nv(vil_in))
        if not cv: skipped+=1; continue
        asha=con.execute('''SELECT u.id FROM users u JOIN worker_villages wv ON wv.user_id=u.id
            WHERE u.role='ASHA' AND wv.village=? LIMIT 1''',(cv,)).fetchone()
        if not asha: skipped+=1; continue
        info=con.execute('SELECT district,block,mobile_connectivity FROM villages WHERE village=?',(cv,)).fetchone()
        pr=(row.get('priority') or 'High').strip().title()
        con.execute('''INSERT INTO worklist(work_date,episode_id,patient_ref,village,district,priority,
            risk_pct,dropout_stage,reason,recommended_action,assigned_cadre,assigned_user_id,
            mobile_connectivity,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (d,row.get('episode_id') or ('U'+str(placed)),row.get('patient_ref') or 'NA',cv,
             info['district'] if info else '',pr,
             float(row.get('risk_pct') or 0) if str(row.get('risk_pct') or '').replace('.','').isdigit() else 0,
             row.get('dropout_stage') or '',row.get('reason') or '',
             row.get('recommended_action') or 'Follow-up call','ASHA',asha['id'],
             info['mobile_connectivity'] if info else '','pending'))
        per_worker[asha['id']]=per_worker.get(asha['id'],0)+1
        placed+=1
    con.commit()
    # readable per-worker summary
    summ=[]
    for uid,n in sorted(per_worker.items(),key=lambda x:-x[1]):
        u=con.execute('SELECT name,block FROM users WHERE id=?',(uid,)).fetchone()
        summ.append({'name':u['name'],'block':u['block'],'cases':n})
    con.close()
    return {'ok':True,'placed':placed,'skipped':skipped,'for_date':d,'assigned':summ}

# ---------- trend / analytics ----------
@app.get('/api/supervisor/analytics')
def analytics(user=Depends(user_from_token)):
    if user['role'] not in ('SUPERVISOR','CHO'):
        raise HTTPException(403,'Supervisor access only')
    con=db(); today=date.today().isoformat()
    by_district=con.execute('''SELECT district,COUNT(*) total,SUM(status='resolved') resolved
        FROM worklist WHERE work_date=? GROUP BY district ORDER BY total DESC''',(today,)).fetchall()
    by_stage=con.execute('''SELECT dropout_stage stage,COUNT(*) n FROM worklist
        WHERE work_date=? GROUP BY dropout_stage ORDER BY n DESC''',(today,)).fetchall()
    by_priority=con.execute('''SELECT priority,COUNT(*) n FROM worklist WHERE work_date=?
        GROUP BY priority''',(today,)).fetchall()
    workload=con.execute('''SELECT u.name,u.block,COUNT(w.id) n FROM users u
        JOIN worklist w ON w.assigned_user_id=u.id AND w.work_date=?
        WHERE u.role='ASHA' GROUP BY u.id ORDER BY n DESC LIMIT 12''',(today,)).fetchall()
    con.close()
    return {'by_district':[dict(r) for r in by_district],
            'by_stage':[dict(r) for r in by_stage],
            'by_priority':[dict(r) for r in by_priority],
            'workload':[dict(r) for r in workload]}

# ---------- serve frontend ----------
@app.get('/')
def index():
    return FileResponse(os.path.join(HERE, 'static', 'index.html'))

app.mount('/static', StaticFiles(directory=os.path.join(HERE,'static')), name='static')
