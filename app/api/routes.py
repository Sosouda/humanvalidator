from fastapi import APIRouter, Depends, HTTPException, Query, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from datetime import date, datetime
from calendar import monthrange
import io
import json
from ..database import get_db
from ..models import Employee, TelegramGroup, Facility, ShiftMark, MessageLog, ProcessingStatus, User
from ..services.processor import process_message, review_message
from ..schemas import IngestMessage
from ..auth import get_current_user, require_admin, require_operator, require_observer
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side

router = APIRouter()
templates_dir = Path(__file__).parent.parent / "web" / "templates"
env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=select_autoescape(['html', 'xml']))
import zoneinfo
from app.config import settings as _settings
from slowapi import Limiter
from slowapi.util import get_remote_address
limiter = Limiter(key_func=get_remote_address)
def _msk(dt):
    if not dt:
        return "—"
    try:
        tz = zoneinfo.ZoneInfo(_settings.timezone)
        if dt.tzinfo is None:
            # считаем что хранится как UTC naive
            dt = dt.replace(tzinfo=zoneinfo.ZoneInfo("UTC"))
        return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S МСК")
    except Exception:
        return str(dt)
env.filters["msk"] = _msk
# также фильтр для короткой даты
env.filters["msk_short"] = lambda dt: _msk(dt)[11:16] if dt else "—"

def render(tpl, **kw):
    return HTMLResponse(env.get_template(tpl).render(**kw))

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db), year: int = None, month: int = None, current_user: User = Depends(require_observer)):
    from datetime import date as d
    today = d.today()
    year = year or today.year
    month = month or today.month
    days_in_month = monthrange(year, month)[1]
    days = list(range(1, days_in_month+1))

    emps = (await db.execute(select(Employee).where(Employee.is_active==True).order_by(Employee.full_name))).scalars().all()
    rows = []
    totals_row = [0]*days_in_month
    for emp in emps:
        all_marks = (await db.execute(select(ShiftMark).where(ShiftMark.employee_id==emp.id))).scalars().all()
        day_map = {}
        total = 0
        for m in all_marks:
            if m.shift_date.year==year and m.shift_date.month==month:
                day_map[m.shift_date.day] = 1
                total += 1
        row = {"employee": emp, "map": day_map, "total": total}
        for di, dnum in enumerate(days):
            if day_map.get(dnum):
                totals_row[di] += 1
        rows.append(row)
    grand_total = sum(totals_row)
    stats_q = await db.execute(select(MessageLog))
    logs = stats_q.scalars().all()
    cnt_accept = sum(1 for l in logs if l.status==ProcessingStatus.accepted.value)
    cnt_manual = sum(1 for l in logs if l.status==ProcessingStatus.manual.value)
    cnt_reject = sum(1 for l in logs if l.status==ProcessingStatus.rejected.value)
    return render("dashboard.html", rows=rows, days=days, year=year, month=month, totals=totals_row, grand_total=grand_total,
                  cnt_accept=cnt_accept, cnt_manual=cnt_manual, cnt_reject=cnt_reject, request=request, current_user=current_user)

@router.get("/journal", response_class=HTMLResponse)
async def journal(request: Request, db: AsyncSession = Depends(get_db), status: str = None, q: str = None, current_user: User = Depends(require_operator)):
    query = select(MessageLog).order_by(MessageLog.date_time.desc()).limit(500)
    logs = (await db.execute(query)).scalars().all()
    if status:
        logs = [l for l in logs if l.status==status]
    if q:
        ql = q.lower()
        logs = [l for l in logs if ql in (l.caption or "").lower() or ql in (l.reason or "").lower()]
    employees = (await db.execute(select(Employee))).scalars().all()
    emp_map = {e.id: e.full_name for e in employees}
    return render("journal.html", logs=logs, emp_map=emp_map, status=status, q=q or "", request=request, current_user=current_user)

@router.get("/manual", response_class=HTMLResponse)
async def manual_queue(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_operator)):
    logs = (await db.execute(select(MessageLog).where(MessageLog.status==ProcessingStatus.manual.value).order_by(MessageLog.date_time.desc()))).scalars().all()
    employees = (await db.execute(select(Employee))).scalars().all()
    return render("manual.html", logs=logs, employees=employees, request=request, current_user=current_user)

@router.post("/manual/{log_id}/review")
async def manual_review_post(request: Request, log_id: int, decision: str = Form(...), employee_id: int = Form(None), shift_date: str = Form(None), db: AsyncSession = Depends(get_db), current_user: User = Depends(require_operator)):
    from datetime import date as d
    sd = None
    if shift_date:
        try:
            sd = d.fromisoformat(shift_date)
        except:
            sd = None
    await review_message(db, log_id, decision, employee_id, sd, reviewer=current_user.username)
    return RedirectResponse("/manual", status_code=303)

@router.get("/api/table")
async def api_table(year: int, month: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_observer)):
    days_in_month = monthrange(year, month)[1]
    emps = (await db.execute(select(Employee))).scalars().all()
    result = []
    for emp in emps:
        marks = (await db.execute(select(ShiftMark).where(ShiftMark.employee_id==emp.id))).scalars().all()
        days = {m.shift_date.day: 1 for m in marks if m.shift_date.year==year and m.shift_date.month==month}
        result.append({"employee": emp.full_name, "days": days, "total": len(days)})
    return result

@router.post("/api/ingest")
@limiter.limit("30/minute")
async def api_ingest(request: Request, payload: IngestMessage, db: AsyncSession = Depends(get_db)):
    log = await process_message(db, payload.model_dump())
    return {"status": log.status, "reason": log.reason, "id": log.id}

@router.get("/export/excel")
async def export_excel(request: Request, year: int = Query(None), month: int = Query(None), db: AsyncSession = Depends(get_db), current_user: User = Depends(require_observer)):
    from datetime import date as d
    today = d.today()
    year = year or today.year
    month = month or today.month
    days_in_month = monthrange(year, month)[1]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{year}-{month:02d}"
    # header
    header = ["Ф.И.О."] + [str(i) for i in range(1, days_in_month+1)] + ["кол.смен"]
    ws.append(header)
    # styles
    bold = Font(bold=True)
    fill_header = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    thin = Side(style="thin", color="B0B0B0")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for col in range(1, len(header)+1):
        c = ws.cell(row=1, column=col)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = fill_header
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = border
    ws.column_dimensions['A'].width = 22
    for i in range(2, days_in_month+2):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = 4
    ws.column_dimensions[openpyxl.utils.get_column_letter(days_in_month+2)].width = 12

    emps = (await db.execute(select(Employee).order_by(Employee.full_name))).scalars().all()
    totals = [0]*days_in_month
    grand = 0
    row_idx = 2
    for emp in emps:
        marks = (await db.execute(select(ShiftMark).where(ShiftMark.employee_id==emp.id))).scalars().all()
        day_map = {m.shift_date.day: 1 for m in marks if m.shift_date.year==year and m.shift_date.month==month}
        row = [emp.full_name] + [day_map.get(i, "") for i in range(1, days_in_month+1)] + [sum(day_map.values())]
        ws.append(row)
        for i in range(1, days_in_month+1):
            if day_map.get(i):
                totals[i-1]+=1
        grand += sum(day_map.values())
        for col in range(1, len(header)+1):
            c = ws.cell(row=row_idx, column=col)
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = border
            if col>1 and col<=days_in_month+1 and c.value==1:
                c.fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
        row_idx+=1
    # итого
    ws.append(["ИТОГ:"] + totals + [grand])
    for col in range(1, len(header)+1):
        c = ws.cell(row=row_idx, column=col)
        c.font = bold
        c.fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
        c.alignment = Alignment(horizontal="center")
        c.border = border
    ws.freeze_panes = "B2"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    fname = f"shifts_{year}_{month:02d}.xlsx"
    return StreamingResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename={fname}"})

@router.get("/groups", response_class=HTMLResponse)
async def groups_page(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_observer)):
    groups = (await db.execute(select(TelegramGroup))).scalars().all()
    facilities = (await db.execute(select(Facility))).scalars().all()
    return render("groups.html", groups=groups, facilities=facilities, request=request, current_user=current_user)

@router.post("/groups/add")
async def groups_add(request: Request, tg_id: int = Form(...), title: str = Form(...), facility_id: int = Form(None), group_type: str = Form("supergroup"), db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    g = TelegramGroup(tg_id=tg_id, title=title, facility_id=facility_id or None, group_type=group_type)
    db.add(g)
    await db.commit()
    return RedirectResponse("/groups", status_code=303)

@router.post("/facilities/add")
async def facilities_add(request: Request, name: str = Form(...), time_windows: str = Form("06:00-12:00"), midnight_cutoff_hour: int = Form(4), auto_mark_allowed: str = Form("on"), db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    allowed = auto_mark_allowed == "on"
    f = Facility(name=name.strip(), time_windows=time_windows.strip() or "06:00-12:00", midnight_cutoff_hour=midnight_cutoff_hour, auto_mark_allowed=allowed)
    db.add(f)
    await db.commit()
    return RedirectResponse("/groups", status_code=303)

@router.post("/facilities/{fid}/toggle")
async def facilities_toggle(request: Request, fid: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    f = await db.get(Facility, fid)
    if not f:
        raise HTTPException(404, "facility not found")
    f.auto_mark_allowed = not f.auto_mark_allowed
    await db.commit()
    return RedirectResponse("/groups", status_code=303)

@router.post("/groups/{gid}/toggle")
async def groups_toggle(request: Request, gid: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    g = await db.get(TelegramGroup, gid)
    if not g:
        raise HTTPException(404, "group not found")
    g.is_enabled = not g.is_enabled
    await db.commit()
    return RedirectResponse("/groups", status_code=303)

@router.get("/employees", response_class=HTMLResponse)
async def employees_page(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_observer)):
    emps = (await db.execute(select(Employee).order_by(Employee.full_name))).scalars().all()
    return render("employees.html", employees=emps, request=request, current_user=current_user)

@router.post("/employees/add")
async def employees_add(request: Request, full_name: str = Form(...), telegram_user_id: str = Form(""), telegram_username: str = Form(""), aliases: str = Form(""), db: AsyncSession = Depends(get_db), current_user: User = Depends(require_admin)):
    tid = int(telegram_user_id) if telegram_user_id.strip().isdigit() else None
    alias_json = json.dumps([a.strip() for a in aliases.split(",") if a.strip()], ensure_ascii=False) if aliases else ""
    e = Employee(full_name=full_name.strip(), telegram_user_id=tid, telegram_username=telegram_username.strip() or None, aliases=alias_json)
    db.add(e)
    await db.commit()
    return RedirectResponse("/employees", status_code=303)

@router.get("/api/stats")
async def api_stats(request: Request, db: AsyncSession = Depends(get_db), current_user: User = Depends(require_observer)):
    logs = (await db.execute(select(MessageLog))).scalars().all()
    from collections import Counter
    c = Counter([l.status for l in logs])
    return c
